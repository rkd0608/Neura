"""Contracts and checkpoints for benchmark and test deployments.

A synthetic ACT deployment is the example SO-101 contracts with the model's real architecture,
random weights (`neura_adapters.synthetic`), the chosen camera resolution, device and precision,
and the artifact's weight manifest computed from the checkpoint. Latency, memory and batching
depend on the architecture and input shapes, not on what the weights learned.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from neura_adapters.synthetic import Size, weights_manifest, write_act_checkpoint
from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, parse_document
from neura_runtime.binding import ExecutionBinding

EXAMPLES = Path(__file__).resolve().parents[4] / "examples" / "contracts"


def example(name: str) -> dict[str, Any]:
    path = EXAMPLES / name
    if not path.is_file():
        raise FileNotFoundError(f"{path}: benchmarks run from a Neura source checkout")
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


@dataclass(frozen=True, slots=True)
class DeploymentFiles:
    artifact: Path
    robot_profile: Path
    execution_profile: Path
    checkpoint: Path | None
    binding: ExecutionBinding


def contracts(
    *,
    image: tuple[int, int] | None = (96, 128),
    device: str = "cpu",
    precision: str = "fp32",
    version: str = "bench-1",
    adapter: str = "lerobot-act",
    weights: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The example ACT/SO-101 documents, edited. ``image`` is (height, width); None keeps the
    example's 480 x 640. ``device`` is "cpu" or "cuda"."""
    artifact = copy.deepcopy(example("act-so101.artifact.yaml"))
    robot = copy.deepcopy(example("so101.robot.yaml"))
    profile = copy.deepcopy(example("act-so101-30hz.profile.yaml"))
    artifact["metadata"]["version"] = version
    artifact["spec"]["adapter"]["name"] = adapter
    if weights is not None:
        artifact["spec"]["weights"] = weights
    if image is not None:
        height, width = image
        for model_input in artifact["spec"]["inputs"]:
            if model_input["kind"] == "image":
                model_input["height"], model_input["width"] = height, width
        for camera in robot["spec"]["cameras"]:
            camera["height"], camera["width"] = height, width
    kind = "cpu" if device == "cpu" else "cuda"
    artifact["spec"]["devices"] = [{"device": kind, "precisions": [precision], "min_memory_mib": 1}]
    profile["metadata"]["version"] = version
    profile["spec"]["runtime"] = {"device": kind, "precision": precision}
    profile["spec"]["binding"] = {
        "model_artifact": parse_document(artifact).digest(),
        "robot_profile": parse_document(robot).digest(),
    }
    return artifact, robot, profile


def write(
    directory: Path,
    documents: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    checkpoint: Path | None = None,
) -> DeploymentFiles:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, document in zip(("artifact", "robot", "profile"), documents, strict=True):
        path = directory / f"{name}.json"
        path.write_text(json.dumps(document, indent=1), encoding="utf-8")
        paths.append(path)
    parsed = [parse_document(d) for d in documents]
    artifact, robot, profile = parsed
    assert isinstance(artifact, ModelArtifact)
    assert isinstance(robot, RobotProfile)
    assert isinstance(profile, ExecutionProfile)
    return DeploymentFiles(
        paths[0], paths[1], paths[2], checkpoint, ExecutionBinding(artifact, robot, profile)
    )


def synthetic_act(
    directory: Path,
    *,
    size: Size = "small",
    image: tuple[int, int] | None = (96, 128),
    device: str = "cpu",
    precision: str = "fp32",
    seed: int = 0,
    version: str | None = None,
) -> DeploymentFiles:
    """A synthetic ACT checkpoint and contracts that bind to it. Different seeds give different
    weights, hence different artifacts: separate fine-tunes of one architecture."""
    version = version or f"synthetic-{size}-{seed}"
    draft = parse_document(contracts(image=image, device=device, precision=precision)[0])
    assert isinstance(draft, ModelArtifact)
    checkpoint = write_act_checkpoint(draft, directory / "checkpoint", size=size, seed=seed)
    documents = contracts(
        image=image,
        device=device,
        precision=precision,
        version=version,
        weights=weights_manifest(checkpoint),
    )
    return write(directory, documents, checkpoint)
