"""The LeRobot adapter on a real ACT network (random weights) on CPU: contract checks at load,
shapes, batching, determinism, and LeRobot's own processors applied (design §12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("lerobot", reason="needs the models group: uv sync --group models")

from neura_adapters import ModelInputs, PolicyError, load_adapter
from neura_adapters.base import blank_inputs
from neura_adapters.synthetic import weights_manifest, write_act_checkpoint
from neura_runtime.binding import ExecutionBinding


def small_cameras(artifact: dict[str, Any], robot: dict[str, Any], profile: dict[str, Any]) -> None:
    for model_input in artifact["spec"]["inputs"]:
        if model_input["kind"] == "image":
            model_input["width"], model_input["height"] = 128, 96
    for camera in robot["spec"]["cameras"]:
        camera["width"], camera["height"] = 128, 96


@pytest.fixture(scope="module")
def checkpoint(tmp_path_factory: pytest.TempPathFactory, binding_factory: Any) -> Path:
    binding: ExecutionBinding = binding_factory(edit=small_cameras)
    return write_act_checkpoint(binding.artifact, tmp_path_factory.mktemp("act"), size="tiny")


def inputs(binding: ExecutionBinding, seed: int) -> ModelInputs:
    rng = np.random.default_rng(seed)
    blank = blank_inputs(binding)
    return ModelInputs(
        state={k: rng.normal(0, 20, v.shape).astype(np.float32) for k, v in blank.state.items()},
        images={k: rng.integers(0, 255, v.shape, dtype=np.uint8) for k, v in blank.images.items()},
        instruction="",
        frozen_prefix=None,
    )


def test_act_runs_batches_deterministically(checkpoint: Path, binding_factory: Any) -> None:
    binding = binding_factory(edit=small_cameras)
    adapter = load_adapter("lerobot-act", binding, checkpoint=checkpoint)
    adapter.warmup()
    assert adapter.properties.deterministic
    assert (adapter.properties.parameters or 0) > 1_000_000
    batch = [inputs(binding, i) for i in range(3)]
    outputs = adapter.infer(batch)
    assert [o.shape for o in outputs] == [(binding.chunk_length, binding.action_dim)] * 3
    assert all(o.dtype == np.float32 and np.isfinite(o).all() for o in outputs)
    # Different observations give different chunks; the same give the same.
    assert not np.allclose(outputs[0], outputs[1])
    again = adapter.infer(batch)
    for a, b in zip(outputs, again, strict=True):
        np.testing.assert_array_equal(a, b)
    # A request's output does not depend on what else is in its batch.
    alone = adapter.infer([batch[1]])[0]
    np.testing.assert_allclose(alone, outputs[1], rtol=1e-4, atol=1e-4)


def test_postprocessor_unnormalizes_actions(checkpoint: Path, binding_factory: Any) -> None:
    binding = binding_factory(edit=small_cameras)
    adapter = load_adapter("lerobot-act", binding, checkpoint=checkpoint)
    # The synthetic checkpoint's action statistics have std 5: unnormalized actions spread wider
    # than the network's raw outputs.
    out = adapter.infer([inputs(binding, 7)])[0]
    assert float(np.abs(out).mean()) > 0.5


def test_checkpoints_that_do_not_match_the_contract_are_refused(
    checkpoint: Path, binding_factory: Any
) -> None:
    def bigger_cameras(a: dict[str, Any], r: dict[str, Any], p: dict[str, Any]) -> None:
        small_cameras(a, r, p)
        a["spec"]["inputs"][1]["width"] = 160
        r["spec"]["cameras"][0]["width"] = 160

    binding = binding_factory(edit=bigger_cameras)
    with pytest.raises(PolicyError, match="has shape"):
        load_adapter("lerobot-act", binding, checkpoint=checkpoint)
    with pytest.raises(PolicyError, match="is a 'act' policy"):
        load_adapter("lerobot-smolvla", binding_factory(edit=small_cameras), checkpoint=checkpoint)
    with pytest.raises(PolicyError, match="not a LeRobot policy checkpoint"):
        load_adapter(
            "lerobot-act", binding_factory(edit=small_cameras), checkpoint=checkpoint.parent
        )


def test_weights_manifest_lists_every_file_with_its_digest(checkpoint: Path) -> None:
    entries = weights_manifest(checkpoint)
    names = {e["path"] for e in entries}
    assert {"config.json", "model.safetensors", "policy_preprocessor.json"} <= names
    assert all(e["digest"].startswith("sha256:") and e["size_bytes"] > 0 for e in entries)


def test_profiler_measures_latency_per_batch_size(checkpoint: Path, binding_factory: Any) -> None:
    from neura_adapters.profiler import profile_adapter

    adapter = load_adapter(
        "lerobot-act", binding_factory(edit=small_cameras), checkpoint=checkpoint
    )
    measured = profile_adapter(adapter, batch_sizes=(1, 4), iterations=5, warmup=1)
    assert [p.batch_size for p in measured.points] == [1, 4]
    for point in measured.points:
        assert 0 < point.p50_us <= point.p99_us <= point.max_us
        assert point.samples == 5
        assert point.memory_mib > 0
    assert measured.memory_source == "process_peak_rss"
    # Four requests in one pass cost less than four passes of one.
    assert measured.points[1].p50_us < 4 * measured.points[0].p50_us
