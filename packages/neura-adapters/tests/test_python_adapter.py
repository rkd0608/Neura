"""The python and container adapters: @neura.policy classes run by policy workers (design
§29.3), loaded from a version's files or from the runtime image, and checked on every call."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from neura_adapters import load_adapter
from neura_adapters.base import ModelInputs, PolicyError, blank_inputs
from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, authoring, parse_document
from neura_contracts.inference import (
    PolicyDeclaration,
    Timing,
    declared_artifact,
    derive_execution_profile,
)
from neura_runtime.binding import ExecutionBinding

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"

HOLD = """
import numpy as np

import neura


@neura.policy(chunk_length=10, cameras={"front": (640, 480)}, weights="weights")
class Hold:
    def __init__(self, weights, device):
        self.offset = float((weights / "offset.txt").read_text())
        self.device = device

    def act(self, observation):
        assert observation["observation.images.front"].shape == (480, 640, 3)
        return np.tile(observation["observation.state"] + self.offset, (10, 1))
"""

BATCHED = HOLD.replace(
    "    def act(self, observation):",
    "    def act_batch(self, observations):\n"
    "        self.batches = getattr(self, 'batches', 0) + 1\n"
    "        return [self.act(o) for o in observations]\n\n"
    "    def act(self, observation):",
)


def robot() -> dict[str, Any]:
    data = yaml.safe_load((EXAMPLES / "so101.robot.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def binding_for(declaration: PolicyDeclaration) -> ExecutionBinding:
    weights = [{"path": "hold.py", "digest": "sha256:" + "a" * 64, "size_bytes": 1}]
    artifact = declared_artifact(declaration, robot(), name="hold", version="1", weights=weights)
    profile, _ = derive_execution_profile(
        artifact, robot(), Timing({1: 5_000}), device="cpu", precision="fp32"
    )
    a, r, p = (parse_document(d) for d in (artifact, robot(), profile))
    assert isinstance(a, ModelArtifact)
    assert isinstance(r, RobotProfile)
    assert isinstance(p, ExecutionProfile)
    return ExecutionBinding(a, r, p)


def version_files(directory: Path, source: str, *, kind: str = "python") -> Path:
    """What a worker's checkout holds for a pushed python version."""
    directory.mkdir(parents=True)
    (directory / "hold.py").write_text(source, encoding="utf-8")
    (directory / "weights").mkdir()
    (directory / "weights" / "offset.txt").write_text("0.5", encoding="utf-8")
    declaration = PolicyDeclaration(adapter=kind, chunk_length=10, cameras={"front": (640, 480)})
    entry = authoring.entry_document(
        kind=kind, declaration=declaration, entry="hold:Hold", weights="weights"
    )
    (directory / authoring.ENTRY_FILE).write_text(json.dumps(entry), encoding="utf-8")
    return directory


def inputs(binding: ExecutionBinding, state: float) -> ModelInputs:
    blank = blank_inputs(binding)
    return ModelInputs(
        state={k: np.full_like(v, state) for k, v in blank.state.items()},
        images=blank.images,
        instruction="",
        frozen_prefix=None,
    )


def test_a_pushed_class_runs_from_the_versions_files(tmp_path: Path) -> None:
    declaration = PolicyDeclaration(
        adapter="python", chunk_length=10, cameras={"front": (640, 480)}
    )
    binding = binding_for(declaration)
    adapter = load_adapter(
        "python", binding, checkpoint=version_files(tmp_path / "v1", HOLD), device="cpu"
    )
    adapter.warmup((1, 2))
    chunks = adapter.infer([inputs(binding, 1.0), inputs(binding, 2.0)])
    assert [c.shape for c in chunks] == [(10, 6), (10, 6)]
    assert chunks[0][0, 0] == pytest.approx(1.5)
    assert chunks[1][0, 0] == pytest.approx(2.5)
    assert adapter.properties.name == "python"
    assert not adapter.properties.batching


def test_act_batch_serves_a_whole_batch_in_one_call(tmp_path: Path) -> None:
    binding = binding_for(
        PolicyDeclaration(adapter="python", chunk_length=10, cameras={"front": (640, 480)})
    )
    adapter = load_adapter("python", binding, checkpoint=version_files(tmp_path / "v", BATCHED))
    assert adapter.properties.batching
    adapter.infer([inputs(binding, 0.0)] * 3)
    assert adapter._policy.batches == 1  # type: ignore[attr-defined]


SHORT_CHUNK = HOLD.replace("(10, 1)", "(3, 1)")
NOT_A_NUMBER = HOLD.replace('observation["observation.state"] + self.offset', "np.full(6, np.nan)")
RAISES = HOLD.replace(
    "        return np.tile", "        raise ValueError('boom')\n        return np.tile"
)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (SHORT_CHUNK, "the policy returned (3, 6); the robot needs (10, 6)"),
        (NOT_A_NUMBER, "NaN or infinite"),
        (RAISES, "the policy failed: ValueError: boom"),
    ],
)
def test_what_the_class_returns_is_checked(tmp_path: Path, source: str, message: str) -> None:
    binding = binding_for(
        PolicyDeclaration(adapter="python", chunk_length=10, cameras={"front": (640, 480)})
    )
    adapter = load_adapter("python", binding, checkpoint=version_files(tmp_path / "v", source))
    with pytest.raises(PolicyError, match=re.escape(message)):
        adapter.infer([inputs(binding, 0.0)])


def test_a_class_that_cannot_load_is_a_load_failure(tmp_path: Path) -> None:
    binding = binding_for(
        PolicyDeclaration(adapter="python", chunk_length=10, cameras={"front": (640, 480)})
    )
    broken = version_files(
        tmp_path / "v", HOLD.replace("float((weights", "float((weights / 'missing'")
    )
    with pytest.raises(PolicyError) as failed:
        load_adapter("python", binding, checkpoint=broken)
    assert failed.value.reason.value == "WORKER_LOAD_FAILED"
    with pytest.raises(PolicyError, match=r"neura-policy\.json"):
        load_adapter("python", binding, checkpoint=tmp_path / "empty")


def test_a_container_runs_the_class_installed_in_its_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The image's Python path: the customer's package, installed.
    package = tmp_path / "site-packages" / "acme_hold"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "policy.py").write_text(
        HOLD.replace(
            'self.offset = float((weights / "offset.txt").read_text())', "self.offset = 0.25"
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path / "site-packages"))
    checkout = tmp_path / "v"
    checkout.mkdir()
    declaration = PolicyDeclaration(
        adapter="container", chunk_length=10, cameras={"front": (640, 480)}
    )
    entry = authoring.entry_document(
        kind="container", declaration=declaration, entry="acme_hold.policy:Hold"
    )
    (checkout / authoring.ENTRY_FILE).write_text(json.dumps(entry), encoding="utf-8")
    binding = binding_for(declaration)
    adapter = load_adapter("container", binding, checkpoint=checkout)
    assert adapter.infer([inputs(binding, 1.0)])[0][0, 0] == pytest.approx(1.25)
