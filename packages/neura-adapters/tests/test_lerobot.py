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
    return write_act_checkpoint(
        binding.artifact, tmp_path_factory.mktemp("act"), size="tiny", action_ranges=ranges(binding)
    )


def ranges(binding: ExecutionBinding) -> list[tuple[float, float]]:
    """The example robot's command ranges: its commands are the model's actions."""
    return [(c.min, c.max) for c in binding.robot.spec.command_interface]


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


def test_loading_never_fetches_pretrained_backbone_weights(
    checkpoint: Path, binding_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Published ACT checkpoints name ImageNet backbone weights in their configuration; the
    checkpoint holds the trained backbone anyway, and a worker has no network."""
    import json
    import shutil

    import torch.hub
    import torchvision.models._api

    published = tmp_path / "published"
    shutil.copytree(checkpoint, published)
    config = json.loads((published / "config.json").read_text())
    config["pretrained_backbone_weights"] = "ResNet18_Weights.IMAGENET1K_V1"
    (published / "config.json").write_text(json.dumps(config))

    def offline(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the worker tried to download weights")

    monkeypatch.setattr(torch.hub, "load_state_dict_from_url", offline)
    monkeypatch.setattr(torchvision.models._api, "load_state_dict_from_url", offline)
    binding = binding_factory(edit=small_cameras)
    batch = [inputs(binding, 7)]
    loaded = load_adapter("lerobot-act", binding, checkpoint=published).infer(batch)[0]
    expected = load_adapter("lerobot-act", binding, checkpoint=checkpoint).infer(batch)[0]
    np.testing.assert_array_equal(loaded, expected)


def test_postprocessor_maps_actions_into_the_robots_ranges(
    checkpoint: Path, binding_factory: Any
) -> None:
    binding = binding_factory(edit=small_cameras)
    adapter = load_adapter("lerobot-act", binding, checkpoint=checkpoint)
    out = adapter.infer([inputs(binding, 7)])[0]
    low = np.array([lo for lo, _ in ranges(binding)])
    high = np.array([hi for _, hi in ranges(binding)])
    assert ((out >= low) & (out <= high)).all()
    # Unnormalized around mid-range: the gripper (0..100) sits near 50, not near 0.
    assert 25 < float(out[:, -1].mean()) < 75


def test_reduced_precision_stays_close_to_fp32(checkpoint: Path, binding_factory: Any) -> None:
    binding = binding_factory(edit=small_cameras)
    full = load_adapter("lerobot-act", binding, checkpoint=checkpoint)
    half = load_adapter("lerobot-act", binding, checkpoint=checkpoint, precision="bf16")
    batch = [inputs(binding, 3), inputs(binding, 4)]
    for a, b in zip(full.infer(batch), half.infer(batch), strict=True):
        assert b.dtype == np.float32
        # bf16 keeps about 3 significant digits; actions span up to 320 degrees.
        np.testing.assert_allclose(b, a, atol=2.0)


def test_diffusion_policy_predicts_chunks_from_one_observation(tmp_path: Path) -> None:
    from neura_bench.deployments import synthetic

    files = synthetic(tmp_path, model="diffusion", size="tiny", image=(48, 64))
    adapter = load_adapter("lerobot-diffusion", files.binding, checkpoint=files.checkpoint)
    assert not adapter.properties.deterministic
    out = adapter.infer([inputs(files.binding, i) for i in range(3)])
    assert [o.shape for o in out] == [(8, 6)] * 3
    low = np.array([lo for lo, _ in ranges(files.binding)])
    high = np.array([hi for _, hi in ranges(files.binding)])
    assert all(((o >= low) & (o <= high)).all() for o in out)


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
    measured = profile_adapter(adapter, batch_sizes=(1, 4), iterations=9, warmup=2)
    assert [p.batch_size for p in measured.points] == [1, 4]
    for point in measured.points:
        assert 0 < point.p50_us <= point.p99_us <= point.max_us
        assert point.samples == 9
        assert point.memory_mib > 0
    assert measured.memory_source == "process_peak_rss"
    # Four requests in one pass cost less than four passes of one.
    assert measured.points[1].p50_us < 4 * measured.points[0].p50_us


def _word_tokenizer(directory: Path) -> Path:
    """A tokenizer made here (no Hub): a few words, a pad token."""
    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import PreTrainedTokenizerFast

    vocab = {"[PAD]": 0, "[UNK]": 1, "pick": 2, "up": 3, "the": 4, "object": 5}
    backend = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    backend.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=backend, pad_token="[PAD]", unk_token="[UNK]"
    )
    tokenizer.save_pretrained(directory)
    return directory


def test_a_gated_tokenizer_is_substituted_and_the_pipeline_says_so(tmp_path: Path) -> None:
    pytest.importorskip("transformers", reason="needs LeRobot's smolvla and pi extras")
    import json

    from lerobot.processor import PolicyProcessorPipeline
    from lerobot.processor.tokenizer_processor import TokenizerProcessorStep

    from neura_adapters.synthetic import (
        PALIGEMMA_TOKENIZER,
        rename_tokenizer,
        substituted_tokenizer,
    )

    local = str(_word_tokenizer(tmp_path / "tokenizer"))
    with substituted_tokenizer(PALIGEMMA_TOKENIZER, local):
        step = TokenizerProcessorStep(tokenizer_name=PALIGEMMA_TOKENIZER, max_length=48)
    # The same fixed number of tokens as pi0 asks of PaliGemma's tokenizer.
    tokens = step._tokenize_text(["pick up the object\n"])
    assert tokens["input_ids"].shape == (1, 48)
    pipeline = PolicyProcessorPipeline(steps=[step], name="policy_preprocessor")
    pipeline.save_pretrained(tmp_path / "checkpoint")
    rename_tokenizer(tmp_path / "checkpoint", PALIGEMMA_TOKENIZER, local)
    saved = json.loads((tmp_path / "checkpoint" / "policy_preprocessor.json").read_text())
    names = [s["config"].get("tokenizer_name") for s in saved["steps"]]
    assert names == [local]  # loading it asks for the tokenizer it was built with
