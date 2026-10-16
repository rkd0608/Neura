"""Contracts for serving an ALOHA policy to the simulated ALOHA: the robot profile (from the
scene's actuators), the model artifact (from the checkpoint's own configuration and files) and an
execution profile that binds them.

The robot's commands are the policy's actions, in the same order and units: six absolute joint
positions in radians and a normalized gripper (0 closed, 1 open) per arm. Joint ranges are the
position actuators' control ranges in the scene. The gripper's command range is wider than its
travel ([-0.25, 1.25]): the actuator clamps a command to the finger's travel, so the range only
refuses what no gripper command should be.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, parse_document
from neura_robots.aloha import DT, JOINTS, AlohaSim
from neura_runtime.binding import ExecutionBinding

API = "neura.dev/v1alpha1"
CONTROL_PERIOD_US = round(DT * 1_000_000)
GRIPPER_RANGE = (-0.25, 1.25)
# Placeholder: the qualification and evidence sites run policy workers as local processes, not
# in the runtime image the artifact pins.
RUNTIME_IMAGE = (
    "registry.example.com/neura/lerobot-worker@sha256:"
    "0000000000000000000000000000000000000000000000000000000000000002"
)


def command_range(sim: AlohaSim, index: int) -> tuple[str, float, float]:
    """The unit and accepted range of command ``index`` (in ``JOINTS`` order)."""
    if JOINTS[index].endswith("_gripper"):
        return ("normalized", *GRIPPER_RANGE)
    arm, joint = divmod(index, 7)  # actuators: 8 per arm, the 6 joints then the two fingers
    low, high = (float(v) for v in sim.model.actuator_ctrlrange[arm * 8 + joint])
    return "rad", low, high


def _features(sim: AlohaSim) -> list[dict[str, Any]]:
    features = []
    for index, name in enumerate(JOINTS):
        unit, low, high = command_range(sim, index)
        semantics = "gripper_position" if unit == "normalized" else "absolute_joint_position"
        features.append(
            {
                "name": name,
                "unit": unit,
                "frame": "joint_space",
                "semantics": semantics,
                "min": round(low, 5),
                "max": round(high, 5),
            }
        )
    return features


def robot_profile(task: str = "transfer_cube") -> dict[str, Any]:
    import mujoco

    sim = AlohaSim(task)  # type: ignore[arg-type]
    commands = _features(sim)
    return {
        "api_version": API,
        "kind": "RobotProfile",
        "metadata": {
            "name": f"aloha-sim-{task.replace('_', '-')}",
            "version": "r1",
            "description": "ALOHA bimanual (two ViperX 300s) in MuJoCo, gym-aloha's scene, "
            "joint position control at 50 Hz, the top camera.",
        },
        "spec": {
            "controller": {"name": "mujoco-position-actuators", "revision": mujoco.__version__},
            "control_period_us": CONTROL_PERIOD_US,
            "command_interface": commands,
            "state_features": [
                {"name": c["name"], "unit": c["unit"], "frame": "joint_space"} for c in commands
            ],
            "cameras": [
                {
                    "role": "top",
                    "width": sim.width,
                    "height": sim.height,
                    "color_order": "rgb",
                    "encodings": ["raw"],
                    "calibration_revision": "gym-aloha-0.1.5",
                }
            ],
            "fault_responses": ["hold_position", "controlled_stop"],
        },
    }


def _weights_manifest(directory: Path) -> list[dict[str, Any]]:
    """``spec.weights``: every file of the checkpoint, with its digest and size."""
    entries = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        entries.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "digest": f"sha256:{digest.hexdigest()}",
                "size_bytes": path.stat().st_size,
            }
        )
    return entries


def artifact(
    checkpoint: Path,
    *,
    name: str,
    source: str,
    device: str = "cpu",
    precision: str = "fp32",
) -> dict[str, Any]:
    """The artifact for a LeRobot ACT checkpoint trained on the ALOHA scene, from its
    ``config.json``: the image inputs it reads (of the top camera), the 14-value state, and its
    chunk length."""
    config = json.loads((checkpoint / "config.json").read_text(encoding="utf-8"))
    if config.get("type") != "act":
        raise ValueError(f"{checkpoint}: an ACT checkpoint is expected, not {config.get('type')}")
    inputs: list[dict[str, Any]] = []
    for key, feature in config["input_features"].items():
        if feature["type"] == "STATE":
            if feature["shape"] != [len(JOINTS)]:
                raise ValueError(f"{key}: shape {feature['shape']}, the ALOHA state has 14")
            inputs.append(
                {
                    "kind": "state",
                    "name": key,
                    "components": [
                        {
                            "name": joint,
                            "unit": "normalized" if joint.endswith("_gripper") else "rad",
                            "frame": "joint_space",
                        }
                        for joint in JOINTS
                    ],
                }
            )
        elif feature["type"] == "VISUAL":
            role = key.rsplit(".", 1)[-1]
            channels, height, width = feature["shape"]
            if role != "top" or channels != 3:
                raise ValueError(f"{key}: the simulated ALOHA serves the top camera (3 channels)")
            inputs.append(
                {
                    "kind": "image",
                    "name": key,
                    "camera_role": role,
                    "width": width,
                    "height": height,
                    "color_order": "rgb",
                    "accepted_encodings": [{"encoding": "raw"}],
                }
            )
        else:
            raise ValueError(f"{key}: input type {feature['type']} is not served")
    sim = AlohaSim()
    outputs = [{k: v for k, v in f.items() if k not in ("min", "max")} for f in _features(sim)]
    kind = "cpu" if device == "cpu" else "cuda"
    return {
        "api_version": API,
        "kind": "ModelArtifact",
        "metadata": {
            "name": name,
            "version": "hub",
            "description": f"LeRobot ACT trained on the ALOHA scene ({source}).",
        },
        "spec": {
            "weights": _weights_manifest(checkpoint),
            "runtime_image": {"reference": RUNTIME_IMAGE},
            "adapter": {"name": "lerobot-act", "version": "0.1.0"},
            "processing": {"preprocessing_version": "0.1.0", "postprocessing_version": "0.1.0"},
            "inputs": inputs,
            "outputs": {
                "chunk_length": int(config["chunk_size"]),
                "control_period_us": CONTROL_PERIOD_US,
                "features": outputs,
            },
            "properties": {
                "deterministic": True,
                "stateful": False,
                "language_conditioned": False,
                "rtc_capable": False,
                "batchable": True,
            },
            "validated_merge_modes": ["replace"],
            "devices": [{"device": kind, "precisions": [precision], "min_memory_mib": 1}],
            "licenses": {
                "code": {"spdx_id": "Apache-2.0", "url": "https://github.com/huggingface/lerobot"},
                "weights": (
                    {"name": source, "url": f"https://huggingface.co/{source}"}
                    if _hub_repo(source)
                    else {"name": source}
                ),
            },
        },
    }


def _hub_repo(source: str) -> bool:
    owner, _, name = source.partition("/")
    return bool(owner and name) and all(c.isalnum() or c in "-_./" for c in source)


@dataclass(frozen=True, slots=True)
class Chunking:
    """How the robot consumes chunks. ``actions_per_chunk`` of each chunk are executed; a new
    chunk is requested when ``refill_at`` actions are left, so the next one is computed while
    these run (``refill_at=0`` waits for the queue to empty, as LeRobot's own loop does)."""

    actions_per_chunk: int = 100
    refill_at: int = 30
    max_age_at_dispatch_us: int = 100_000
    max_age_at_acceptance_us: int = 1_500_000
    request_timeout_us: int = 2_000_000
    starvation_grace_us: int = 1_000_000


def execution_profile(
    artifact_document: dict[str, Any],
    robot_document: dict[str, Any],
    *,
    chunking: Chunking = Chunking(),  # noqa: B008 - frozen
    device: str = "cpu",
    precision: str = "fp32",
) -> dict[str, Any]:
    chunk_length = artifact_document["spec"]["outputs"]["chunk_length"]
    per_chunk = min(chunking.actions_per_chunk, chunk_length)
    return {
        "api_version": API,
        "kind": "ExecutionProfile",
        "metadata": {
            "name": f"{artifact_document['metadata']['name']}-50hz",
            "version": (
                f"chunk{per_chunk}-refill{chunking.refill_at}"
                f"-dispatch{chunking.max_age_at_dispatch_us // 1000}ms"
            ),
            "description": "ACT on the simulated ALOHA at 50 Hz, replace merge.",
        },
        "spec": {
            "binding": {
                "model_artifact": parse_document(artifact_document).digest(),
                "robot_profile": parse_document(robot_document).digest(),
            },
            "control_period_us": CONTROL_PERIOD_US,
            "chunk": {
                "actions_per_chunk": per_chunk,
                "refill_threshold_actions": chunking.refill_at,
                "min_useful_actions": min(10, per_chunk),
            },
            "merge": {"mode": "replace"},
            "freshness": {
                "max_observation_age_at_dispatch_us": chunking.max_age_at_dispatch_us,
                "max_observation_age_at_acceptance_us": chunking.max_age_at_acceptance_us,
                "request_timeout_us": chunking.request_timeout_us,
            },
            "starvation": {
                "response": "hold_position",
                "grace_us": chunking.starvation_grace_us,
                "require_new_epoch_on_resume": True,
            },
            "runtime": {"device": "cpu" if device == "cpu" else "cuda", "precision": precision},
            "image_encodings": [{"camera_role": "top", "encoding": "raw"}],
        },
    }


@dataclass(frozen=True, slots=True)
class Deployment:
    artifact: Path
    robot_profile: Path
    execution_profile: Path
    checkpoint: Path
    binding: ExecutionBinding


def write(
    directory: Path,
    checkpoint: Path,
    *,
    name: str,
    source: str,
    task: str = "transfer_cube",
    chunking: Chunking = Chunking(),  # noqa: B008 - frozen
    device: str = "cpu",
    precision: str = "fp32",
) -> Deployment:
    """Write the three documents for serving ``checkpoint`` and return their paths."""
    robot = robot_profile(task)
    model = artifact(checkpoint, name=name, source=source, device=device, precision=precision)
    profile = execution_profile(model, robot, chunking=chunking, device=device, precision=precision)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for stem, document in (("artifact", model), ("robot", robot), ("profile", profile)):
        path = directory / f"{stem}.json"
        path.write_text(json.dumps(document, indent=1), encoding="utf-8")
        paths.append(path)
    parsed = [parse_document(copy.deepcopy(d)) for d in (model, robot, profile)]
    a, r, p = parsed
    assert isinstance(a, ModelArtifact)
    assert isinstance(r, RobotProfile)
    assert isinstance(p, ExecutionProfile)
    return Deployment(paths[0], paths[1], paths[2], checkpoint, ExecutionBinding(a, r, p))
