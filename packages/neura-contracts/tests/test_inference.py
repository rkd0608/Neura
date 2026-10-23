from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from neura_contracts import (
    ExecutionProfile,
    ModelArtifact,
    RobotProfile,
    check_compatibility,
    parse_document,
)
from neura_contracts.inference import (
    InferenceError,
    PolicyDeclaration,
    Timing,
    declared_artifact,
    derive_execution_profile,
    derive_limits,
    lerobot_artifact,
    nominal_timing,
    read_lerobot_config,
    weights_manifest,
)

Data = dict[str, Any]


def act_config(**overrides: Any) -> Data:
    """A LeRobot ACT config.json for an SO-101 with two 640x480 cameras."""
    config: Data = {
        "type": "act",
        "chunk_size": 100,
        "n_action_steps": 100,
        "input_features": {
            "observation.state": {"type": "STATE", "shape": [6]},
            "observation.images.front": {"type": "VISUAL", "shape": [3, 480, 640]},
            "observation.images.wrist": {"type": "VISUAL", "shape": [3, 480, 640]},
        },
        "output_features": {"action": {"type": "ACTION", "shape": [6]}},
    }
    config.update(overrides)
    return config


WEIGHTS = [{"path": "model.safetensors", "digest": "sha256:" + "a" * 64, "size_bytes": 1 << 20}]


def checked(artifact: Data, robot: Data, profile: Data) -> None:
    a, r, p = (parse_document(copy.deepcopy(d)) for d in (artifact, robot, profile))
    assert isinstance(a, ModelArtifact)
    assert isinstance(r, RobotProfile)
    assert isinstance(p, ExecutionProfile)
    report = check_compatibility(a, r, p)
    assert report.compatible, report.issues


def test_act_checkpoint_serves_the_so101(robot_data: Data) -> None:
    artifact = lerobot_artifact(
        act_config(), robot_data, name="pick", version="v1", weights=WEIGHTS
    )
    spec = artifact["spec"]
    assert spec["adapter"]["name"] == "lerobot-act"
    assert spec["outputs"]["chunk_length"] == 100
    assert spec["outputs"]["control_period_us"] == robot_data["spec"]["control_period_us"]
    state = next(i for i in spec["inputs"] if i["kind"] == "state")
    assert [c["name"] for c in state["components"]] == [
        s["name"] for s in robot_data["spec"]["state_features"]
    ]
    assert spec["properties"]["deterministic"] is True
    assert spec["properties"]["language_conditioned"] is False
    profile, limits = derive_execution_profile(
        artifact, robot_data, Timing({1: 40_000, 4: 70_000}), device="cuda", precision="bf16"
    )
    assert limits.measured
    checked(artifact, robot_data, profile)


def test_language_models_get_a_language_input(robot_data: Data) -> None:
    config = act_config(type="smolvla", chunk_size=50, n_action_steps=50)
    artifact = lerobot_artifact(config, robot_data, name="vla", version="v1", weights=WEIGHTS)
    assert artifact["spec"]["properties"]["language_conditioned"] is True
    assert artifact["spec"]["properties"]["deterministic"] is False
    assert any(i["kind"] == "language" for i in artifact["spec"]["inputs"])
    # 400 ms a pass: 30 Hz with 50-action chunks still fits one robot.
    profile, _ = derive_execution_profile(
        artifact, robot_data, Timing({1: 400_000}), device="cuda", precision="bf16"
    )
    assert profile["spec"]["language"] == {"max_length_bytes": 256}
    checked(artifact, robot_data, profile)


def test_diffusion_returns_its_action_steps(robot_data: Data) -> None:
    config = act_config(type="diffusion", horizon=16, n_action_steps=8, chunk_size=None)
    artifact = lerobot_artifact(config, robot_data, name="d", version="v1", weights=WEIGHTS)
    assert artifact["spec"]["outputs"]["chunk_length"] == 8


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (act_config(type="tdmpc"), "not supported yet"),
        (
            act_config(input_features={"observation.state": {"type": "STATE", "shape": [14]}}),
            "state has 14 values",
        ),
        (
            act_config(
                input_features={
                    "observation.state": {"type": "STATE", "shape": [6]},
                    "observation.images.top": {"type": "VISUAL", "shape": [3, 480, 640]},
                }
            ),
            "reads camera 'top'",
        ),
        (act_config(output_features={"action": {"type": "ACTION", "shape": [7]}}), "sends 7"),
    ],
)
def test_mismatches_say_what_to_fix(robot_data: Data, config: Data, message: str) -> None:
    with pytest.raises(InferenceError) as raised:
        lerobot_artifact(config, robot_data, name="x", version="v1", weights=WEIGHTS)
    assert message in str(raised.value)


def test_limits_grow_with_the_pass(robot_data: Data) -> None:
    artifact = lerobot_artifact(act_config(), robot_data, name="p", version="v1", weights=WEIGHTS)
    fast = derive_limits(artifact, robot_data, Timing({1: 30_000}))
    slow = derive_limits(artifact, robot_data, Timing({1: 300_000}))
    assert fast.refill_threshold_actions < slow.refill_threshold_actions
    assert fast.max_observation_age_at_dispatch_us == 100_000  # the floor
    assert slow.max_observation_age_at_dispatch_us > fast.max_observation_age_at_dispatch_us
    # The next chunk is asked early enough for two passes, the round trip and a margin.
    period = robot_data["spec"]["control_period_us"]
    assert slow.refill_threshold_actions * period >= 2 * 300_000 + 20_000 + 20_000


def test_more_robots_use_the_larger_batch(robot_data: Data) -> None:
    artifact = lerobot_artifact(act_config(), robot_data, name="p", version="v1", weights=WEIGHTS)
    timing = Timing({1: 40_000, 4: 80_000})
    assert derive_limits(artifact, robot_data, timing, robots=1).pass_p99_us == 40_000
    assert derive_limits(artifact, robot_data, timing, robots=3).pass_p99_us == 80_000
    assert derive_limits(artifact, robot_data, timing, robots=8).pass_p99_us == 160_000


def test_no_fit_explains_why(robot_data: Data) -> None:
    config = act_config(chunk_size=20, n_action_steps=20)
    artifact = lerobot_artifact(config, robot_data, name="p", version="v1", weights=WEIGHTS)
    with pytest.raises(InferenceError) as raised:
        derive_limits(artifact, robot_data, Timing({1: 900_000}))
    assert "faster GPU" in str(raised.value)


def test_nominal_timing_is_marked_estimated() -> None:
    gpu = nominal_timing("lerobot-act", "cuda")
    cpu = nominal_timing("lerobot-act", "cpu")
    assert not gpu.measured
    assert cpu.pass_p99_us(1) > gpu.pass_p99_us(1)


def test_reads_a_checkpoint_directory(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(json.dumps(act_config()), encoding="utf-8")
    (tmp_path / "model.safetensors").write_bytes(b"weights")
    assert read_lerobot_config(tmp_path)["type"] == "act"
    files = weights_manifest(tmp_path)
    assert [f["path"] for f in files] == ["config.json", "model.safetensors"]
    with pytest.raises(InferenceError, match=r"no config\.json"):
        read_lerobot_config(tmp_path / "missing")


@pytest.mark.parametrize("adapter", ["python", "container"])
def test_declared_policies_serve_like_checkpoints(robot_data: Data, adapter: str) -> None:
    declaration = PolicyDeclaration(
        adapter=adapter,
        chunk_length=50,
        cameras={"front": (640, 480)},
        deterministic=True,
    )
    artifact = declared_artifact(declaration, robot_data, name="own", version="v1", weights=WEIGHTS)
    spec = artifact["spec"]
    assert spec["adapter"]["name"] == adapter
    assert [i["name"] for i in spec["inputs"]] == ["observation.state", "observation.images.front"]
    assert spec["properties"]["stateful"] is False
    profile, _ = derive_execution_profile(
        artifact, robot_data, Timing({1: 30_000}), device="cuda", precision="bf16"
    )
    checked(artifact, robot_data, profile)


def test_declaration_reads_every_robot_camera_by_default(robot_data: Data) -> None:
    declaration = PolicyDeclaration(adapter="python", chunk_length=10)
    artifact = declared_artifact(declaration, robot_data, name="all", version="v1", weights=WEIGHTS)
    roles = [i["camera_role"] for i in artifact["spec"]["inputs"] if i["kind"] == "image"]
    assert roles == sorted(c["role"] for c in robot_data["spec"]["cameras"])
    assert PolicyDeclaration.from_json(declaration.to_json()) == declaration


def test_declaration_names_missing_cameras(robot_data: Data) -> None:
    declaration = PolicyDeclaration(adapter="container", chunk_length=10, cameras={"top": (64, 64)})
    with pytest.raises(InferenceError, match="reads camera 'top'"):
        declared_artifact(declaration, robot_data, name="x", version="v1", weights=WEIGHTS)
