"""Contracts for serving an ALOHA policy to the simulated ALOHA: the robot profile (from the
scene's actuators), the model artifact (inferred from the checkpoint's own configuration and
files, as for any LeRobot checkpoint) and an execution profile that binds them.

The robot's commands are the policy's actions, in the same order and units: six absolute joint
positions in radians and a normalized gripper (0 closed, 1 open) per arm. Joint ranges are the
position actuators' control ranges in the scene (``AlohaSim._command``).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, parse_document
from neura_contracts.inference import lerobot_artifact, read_lerobot_config, weights_manifest
from neura_robots.aloha import DT, AlohaSim
from neura_robots.sim.scene import robot_profile as scene_profile
from neura_runtime.binding import ExecutionBinding

API = "neura.dev/v1alpha1"
CONTROL_PERIOD_US = round(DT * 1_000_000)


def robot_profile(task: str = "transfer_cube") -> dict[str, Any]:
    return scene_profile(
        AlohaSim(task),  # type: ignore[arg-type]
        name=f"aloha-sim-{task.replace('_', '-')}",
        description="ALOHA bimanual (two ViperX 300s) in MuJoCo, gym-aloha's scene, "
        "joint position control at 50 Hz, the top camera.",
        calibration="gym-aloha-0.1.5",
    )


def artifact(
    checkpoint: Path, *, name: str, source: str, task: str = "transfer_cube"
) -> dict[str, Any]:
    """The artifact of a LeRobot ACT checkpoint trained on the ALOHA scene, inferred from its
    ``config.json`` for the simulated ALOHA's robot profile (``neura_contracts.inference``), its
    weights credited to ``source``."""
    config = read_lerobot_config(checkpoint)
    if config.get("type") != "act":
        raise ValueError(f"{checkpoint}: an ACT checkpoint is expected, not {config.get('type')}")
    license_ = {"name": source}
    if _hub_repo(source):
        license_["url"] = f"https://huggingface.co/{source}"
    return lerobot_artifact(
        config,
        robot_profile(task),
        name=name,
        version="hub",
        weights=weights_manifest(checkpoint),
        weights_license=license_,
    )


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
    model = artifact(checkpoint, name=name, source=source, task=task)
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
