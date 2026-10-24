"""Contract documents inferred, so that nobody writes them by hand (design §29).

- ``declared_artifact``: the model artifact of any policy, from a ``PolicyDeclaration`` (what it
  reads and returns) and the robot profile it is for. A LeRobot checkpoint declares itself in its
  ``config.json`` (``lerobot_declaration``); a Python class with ``@neura.policy`` or a container
  declares the same fields in code. Policies name their inputs and shapes, not the robot's joints
  or units, so state components and action features come from the robot profile, matched by
  position.
- ``derive_execution_profile``: the execution profile that binds an artifact to a robot, with
  timing limits derived from a measured (or, before any measurement, a nominal) forward pass.

Both return plain documents, ready for ``parse_document``. Neither imports LeRobot or PyTorch.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neura_contracts.documents import parse_document

API = "neura.dev/v1alpha1"

# The worker image artifacts pin when none is given. Sites that run workers as processes (the
# default outside a container sandbox) never pull it; NEURA_RUNTIME_IMAGE names the image a site's
# container sandbox runs.
PLACEHOLDER_RUNTIME_IMAGE = (
    "registry.nucleilabs.ai/neura/lerobot-worker@sha256:"
    "0000000000000000000000000000000000000000000000000000000000000002"
)

# LeRobot policy type -> Neura adapter (neura_adapters.base).
LEROBOT_ADAPTERS = {
    "act": "lerobot-act",
    "smolvla": "lerobot-smolvla",
    "pi0": "lerobot-pi0",
    "pi05": "lerobot-pi05",
    "diffusion": "lerobot-diffusion",
}
_DETERMINISTIC = {"act"}
_LANGUAGE = {"smolvla", "pi0", "pi05"}
LANGUAGE_INPUT = "task"
LANGUAGE_MAX_BYTES = 512

# A forward pass before any measurement, by policy type (p99, one observation, on a GPU). Only a
# first estimate: the site measures the real one before a rollout goes on (design §29.3).
NOMINAL_PASS_US = {
    "lerobot-act": 50_000,
    "lerobot-diffusion": 150_000,
    "lerobot-smolvla": 450_000,
    "lerobot-pi0": 450_000,
    "lerobot-pi05": 450_000,
    "mock": 20_000,
}
DEFAULT_NOMINAL_PASS_US = 250_000
CPU_SLOWDOWN = 4
DEFAULT_RTT_P99_US = 20_000
MARGIN_US = 20_000


class InferenceError(ValueError):
    """The checkpoint cannot serve this robot, or no timing fits. ``problems`` are sentences a
    person can act on."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def read_lerobot_config(checkpoint: Path) -> dict[str, Any]:
    path = checkpoint / "config.json"
    if not path.is_file():
        raise InferenceError([f"{checkpoint} has no config.json: is it a LeRobot checkpoint?"])
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise InferenceError([f"{path} is not JSON: {error}"]) from error
    if not isinstance(config, dict) or "type" not in config:
        raise InferenceError([f"{path} names no policy type"])
    return config


def weights_manifest(directory: Path) -> list[dict[str, Any]]:
    """``spec.weights``: every file under ``directory``, with its digest and size."""
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
    if not entries:
        raise InferenceError([f"{directory} has no files"])
    return entries


def _spec(document: Mapping[str, Any]) -> Mapping[str, Any]:
    spec = document.get("spec")
    if not isinstance(spec, Mapping):
        raise InferenceError(["the document has no spec"])
    return spec


def _executed_actions(config: Mapping[str, Any]) -> tuple[int, int]:
    """(chunk length the model returns, actions of each chunk its trainer executed)."""
    kind = config["type"]
    chunk = config.get("n_action_steps") if kind == "diffusion" else config.get("chunk_size")
    if not isinstance(chunk, int) or chunk < 1:
        chunk = config.get("n_action_steps")
    if not isinstance(chunk, int) or chunk < 1:
        raise InferenceError([f"the {kind} checkpoint gives no chunk size"])
    executed = config.get("n_action_steps")
    if not isinstance(executed, int) or executed < 1:
        executed = chunk
    return chunk, min(executed, chunk)


@dataclass(frozen=True, slots=True)
class PolicyDeclaration:
    """What a policy reads and returns, however it is built: the same for a LeRobot checkpoint,
    a Python class (``@neura.policy``) and a container. Inputs follow LeRobot's names
    (``observation.state``, ``observation.images.<role>``, ``task``), so one convention covers
    every source.

    ``cameras`` maps each camera role the policy reads to its (width, height); ``None`` reads
    every camera of the robot at its own size."""

    adapter: str
    chunk_length: int
    executed_actions: int | None = None
    cameras: Mapping[str, tuple[int, int]] | None = None
    state: bool = True
    language: bool = False
    deterministic: bool = False
    stateful: bool = False
    batchable: bool = True
    description: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "chunk_length": self.chunk_length,
            "executed_actions": self.executed_actions,
            "cameras": None
            if self.cameras is None
            else {role: list(size) for role, size in self.cameras.items()},
            "state": self.state,
            "language": self.language,
            "deterministic": self.deterministic,
            "stateful": self.stateful,
            "batchable": self.batchable,
            "description": self.description,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PolicyDeclaration:
        cameras = data.get("cameras")
        return cls(
            adapter=str(data["adapter"]),
            chunk_length=int(data["chunk_length"]),
            executed_actions=data.get("executed_actions"),
            cameras=None
            if cameras is None
            else {str(r): (int(s[0]), int(s[1])) for r, s in dict(cameras).items()},
            state=bool(data.get("state", True)),
            language=bool(data.get("language", False)),
            deterministic=bool(data.get("deterministic", False)),
            stateful=bool(data.get("stateful", False)),
            batchable=bool(data.get("batchable", True)),
            description=str(data.get("description", "")),
        )


def lerobot_declaration(config: Mapping[str, Any], robot: Mapping[str, Any]) -> PolicyDeclaration:
    """The declaration a LeRobot checkpoint's ``config.json`` makes, checked against ``robot``."""
    kind = config.get("type")
    adapter = LEROBOT_ADAPTERS.get(str(kind))
    if adapter is None:
        raise InferenceError(
            [
                f"LeRobot policy type {kind!r} is not supported yet; supported: "
                + ", ".join(sorted(LEROBOT_ADAPTERS))
                + ". Any other model can be wrapped in a @neura.policy class"
            ]
        )
    spec = _spec(robot)
    robot_name = robot.get("metadata", {}).get("name", "the robot")
    state_features = list(spec.get("state_features", []))
    commands = list(spec.get("command_interface", []))
    problems: list[str] = []
    state = False
    cameras: dict[str, tuple[int, int]] = {}
    for key, feature in sorted(dict(config.get("input_features", {})).items()):
        shape = list(feature.get("shape", []))
        if feature.get("type") == "STATE":
            if key != "observation.state":
                problems.append(f"input {key!r}: only observation.state is served as robot state")
            elif shape != [len(state_features)]:
                problems.append(
                    f"the checkpoint's state has {shape[0] if shape else 0} values; "
                    f"{robot_name} reports {len(state_features)}"
                )
            else:
                state = True
        elif feature.get("type") == "VISUAL":
            if len(shape) != 3 or shape[0] != 3:
                problems.append(f"input {key!r}: an RGB image is expected, not shape {shape}")
            else:
                cameras[key.rsplit(".", 1)[-1]] = (int(shape[2]), int(shape[1]))
        else:
            problems.append(f"input {key!r} of type {feature.get('type')!r} is not served")
    action = dict(config.get("output_features", {})).get("action")
    action_shape = list(action.get("shape", [])) if isinstance(action, Mapping) else []
    if action_shape != [len(commands)]:
        problems.append(
            f"the checkpoint sends {action_shape[0] if action_shape else 0} commands; "
            f"{robot_name} takes {len(commands)}"
        )
    chunk, executed = 1, None
    try:
        chunk, executed = _executed_actions(config)
    except InferenceError as error:
        problems.extend(error.problems)
    if problems:
        raise InferenceError(problems)
    return PolicyDeclaration(
        adapter=adapter,
        chunk_length=chunk,
        executed_actions=executed,
        cameras=cameras,
        state=state,
        language=kind in _LANGUAGE,
        deterministic=kind in _DETERMINISTIC,
        description=f"LeRobot {kind} for {robot_name}.",
    )


def declared_artifact(
    declaration: PolicyDeclaration,
    robot: Mapping[str, Any],
    *,
    name: str,
    version: str,
    weights: list[dict[str, Any]],
    runtime_image: str | None = None,
    weights_license: str | None = None,
    code_license: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The model artifact of any policy, from its declaration and the robot it is for. State
    components and action features are the robot's, in its order and units."""
    spec = _spec(robot)
    robot_name = robot.get("metadata", {}).get("name", "the robot")
    robot_cameras = {c["role"]: c for c in spec.get("cameras", [])}
    problems: list[str] = []
    inputs: list[dict[str, Any]] = []
    if declaration.state:
        inputs.append(
            {
                "kind": "state",
                "name": "observation.state",
                "components": [
                    {"name": s["name"], "unit": s["unit"], "frame": s["frame"]}
                    for s in spec.get("state_features", [])
                ],
            }
        )
    wanted = (
        {role: (int(c["width"]), int(c["height"])) for role, c in robot_cameras.items()}
        if declaration.cameras is None
        else dict(declaration.cameras)
    )
    for role, (width, height) in sorted(wanted.items()):
        camera = robot_cameras.get(role)
        if camera is None:
            problems.append(
                f"the policy reads camera {role!r}; {robot_name} has "
                + (", ".join(sorted(robot_cameras)) if robot_cameras else "no cameras")
            )
            continue
        inputs.append(
            {
                "kind": "image",
                "name": f"observation.images.{role}",
                "camera_role": role,
                "width": width,
                "height": height,
                "color_order": camera.get("color_order", "rgb"),
                "accepted_encodings": [{"encoding": "raw"}],
            }
        )
    if declaration.language:
        inputs.append(
            {"kind": "language", "name": LANGUAGE_INPUT, "max_length_bytes": LANGUAGE_MAX_BYTES}
        )
    if not inputs:
        problems.append("the policy reads nothing: declare state, cameras or language")
    if declaration.chunk_length < 1:
        problems.append("a chunk has at least one action")
    if problems:
        raise InferenceError(problems)
    commands = list(spec.get("command_interface", []))
    size_mib = sum(int(w.get("size_bytes", 0)) for w in weights) / (1 << 20)
    memory = max(256, math.ceil(size_mib * 2))
    return {
        "api_version": API,
        "kind": "ModelArtifact",
        "metadata": {
            "name": name,
            "version": version,
            "description": declaration.description or f"{declaration.adapter} for {robot_name}.",
        },
        "spec": {
            "weights": weights,
            "runtime_image": {
                "reference": runtime_image
                or os.environ.get("NEURA_RUNTIME_IMAGE")
                or PLACEHOLDER_RUNTIME_IMAGE
            },
            "adapter": {"name": declaration.adapter, "version": "0.1.0"},
            "processing": {"preprocessing_version": "0.1.0", "postprocessing_version": "0.1.0"},
            "inputs": inputs,
            "outputs": {
                "chunk_length": declaration.chunk_length,
                "control_period_us": int(spec["control_period_us"]),
                "features": [
                    {k: c[k] for k in ("name", "unit", "frame", "semantics")} for c in commands
                ],
            },
            "properties": {
                "deterministic": declaration.deterministic,
                "stateful": declaration.stateful,
                "language_conditioned": declaration.language,
                "rtc_capable": False,
                "batchable": declaration.batchable,
            },
            "validated_merge_modes": ["replace"],
            "devices": [
                {"device": "cuda", "precisions": ["fp32", "bf16"], "min_memory_mib": memory},
                {"device": "cpu", "precisions": ["fp32"], "min_memory_mib": memory},
            ],
            "licenses": {
                "code": code_license
                or {"spdx_id": "Apache-2.0", "url": "https://github.com/huggingface/lerobot"},
                "weights": {"name": weights_license or "Customer-owned"},
            },
        },
    }


def lerobot_artifact(
    config: Mapping[str, Any],
    robot: Mapping[str, Any],
    *,
    name: str,
    version: str,
    weights: list[dict[str, Any]],
    runtime_image: str | None = None,
    weights_license: str | None = None,
) -> dict[str, Any]:
    """The model artifact of a LeRobot checkpoint (its ``config.json``) for ``robot``."""
    return declared_artifact(
        lerobot_declaration(config, robot),
        robot,
        name=name,
        version=version,
        weights=weights,
        runtime_image=runtime_image,
        weights_license=weights_license,
    )


@dataclass(frozen=True, slots=True)
class Timing:
    """A forward pass as measured at the site: p99 in µs by batch size."""

    pass_p99_us_by_batch: Mapping[int, int] = field(default_factory=dict)
    rtt_p99_us: int = DEFAULT_RTT_P99_US
    measured: bool = True

    def pass_p99_us(self, robots: int) -> int:
        if not self.pass_p99_us_by_batch:
            raise InferenceError(["no forward pass was measured"])
        sizes = sorted(self.pass_p99_us_by_batch)
        fitting = [b for b in sizes if b >= robots]
        size = fitting[0] if fitting else sizes[-1]
        value = int(self.pass_p99_us_by_batch[size])
        if not fitting:  # more robots than any measured batch: scale linearly
            value = math.ceil(value * robots / size)
        return value


def nominal_timing(adapter: str, device: str) -> Timing:
    """A first estimate of the forward pass, before the site measures one."""
    p99 = NOMINAL_PASS_US.get(adapter, DEFAULT_NOMINAL_PASS_US)
    if device == "cpu":
        p99 *= CPU_SLOWDOWN
    return Timing({1: p99}, measured=False)


def provisional_timing(
    artifact: Mapping[str, Any],
    robot: Mapping[str, Any],
    *,
    device: str,
    robots: int = 1,
    executed_actions: int | None = None,
) -> Timing:
    """The timing a rollout prepares with before the site has measured anything: the nominal
    estimate for the policy's type or, when that would not fit, the slowest pass that would. An
    estimate never refuses a policy; the site's measurement decides (``derive_limits``)."""
    adapter = str(_spec(artifact)["adapter"]["name"])
    timing = nominal_timing(adapter, device)
    try:
        derive_limits(artifact, robot, timing, robots=robots, executed_actions=executed_actions)
    except InferenceError:
        period = int(_spec(robot)["control_period_us"])
        chunk = int(_spec(artifact)["outputs"]["chunk_length"])
        per_chunk = min(chunk, executed_actions or chunk)
        # refill = ceil((2 * pass + rtt + margin) / period) + 1 must stay below per_chunk.
        fitting = ((per_chunk - 2) * period - timing.rtt_p99_us - MARGIN_US) // 2 - 1
        if fitting // robots < 1:
            raise
        return Timing({1: fitting // robots}, measured=False)
    return timing


@dataclass(frozen=True, slots=True)
class Limits:
    """The timing limits of an execution profile, and how they were derived."""

    actions_per_chunk: int
    refill_threshold_actions: int
    min_useful_actions: int
    max_observation_age_at_dispatch_us: int
    max_observation_age_at_acceptance_us: int
    request_timeout_us: int
    pass_p99_us: int
    measured: bool

    def summary(self) -> str:
        source = "measured" if self.measured else "estimated"
        return (
            f"{self.pass_p99_us / 1000:.0f} ms per pass ({source}); next chunk asked "
            f"{self.refill_threshold_actions} actions early"
        )


def derive_limits(
    artifact: Mapping[str, Any],
    robot: Mapping[str, Any],
    timing: Timing,
    *,
    robots: int = 1,
    executed_actions: int | None = None,
) -> Limits:
    """Timing limits that let ``robots`` robots share the measured pass without starving.

    The next chunk is asked for early enough to cover two passes (one possibly queued behind
    another robot's batch), the round trip and a margin. An observation may wait up to one pass
    before it is dispatched, and a chunk may arrive up to two passes after that."""
    period = int(_spec(robot)["control_period_us"])
    chunk = int(_spec(artifact)["outputs"]["chunk_length"])
    per_chunk = min(chunk, executed_actions or chunk)
    pass_us = timing.pass_p99_us(robots)
    rtt = timing.rtt_p99_us
    needed = 2 * pass_us + rtt + MARGIN_US
    refill = math.ceil(needed / period) + 1
    if refill >= per_chunk:
        raise InferenceError(
            [
                f"a pass takes {pass_us / 1000:.0f} ms for {robots} robot(s), so the next chunk "
                f"would have to be asked {refill} actions early, but a chunk has only "
                f"{per_chunk}: use a faster GPU, fewer robots per GPU, or a model with longer "
                "chunks"
            ]
        )
    dispatch = max(100_000, pass_us + rtt + MARGIN_US)
    acceptance = max(150_000, dispatch + 2 * pass_us + rtt + MARGIN_US)
    return Limits(
        actions_per_chunk=per_chunk,
        refill_threshold_actions=refill,
        min_useful_actions=max(1, min(per_chunk - refill, per_chunk // 5)),
        max_observation_age_at_dispatch_us=dispatch,
        max_observation_age_at_acceptance_us=acceptance,
        request_timeout_us=acceptance + 50_000,
        pass_p99_us=pass_us,
        measured=timing.measured,
    )


def derive_execution_profile(
    artifact: Mapping[str, Any],
    robot: Mapping[str, Any],
    timing: Timing,
    *,
    device: str,
    precision: str,
    robots: int = 1,
    executed_actions: int | None = None,
) -> tuple[dict[str, Any], Limits]:
    """The execution profile binding ``artifact`` to ``robot``, with limits from ``timing``."""
    limits = derive_limits(
        artifact, robot, timing, robots=robots, executed_actions=executed_actions
    )
    artifact_spec = _spec(artifact)
    images = [i for i in artifact_spec["inputs"] if i["kind"] == "image"]
    language = [i for i in artifact_spec["inputs"] if i["kind"] == "language"]
    spec: dict[str, Any] = {
        "binding": {
            "model_artifact": parse_document(dict(artifact)).digest(),
            "robot_profile": parse_document(dict(robot)).digest(),
        },
        "control_period_us": int(_spec(robot)["control_period_us"]),
        "chunk": {
            "actions_per_chunk": limits.actions_per_chunk,
            "refill_threshold_actions": limits.refill_threshold_actions,
            "min_useful_actions": limits.min_useful_actions,
        },
        "merge": {"mode": "replace"},
        "freshness": {
            "max_observation_age_at_dispatch_us": limits.max_observation_age_at_dispatch_us,
            "max_observation_age_at_acceptance_us": limits.max_observation_age_at_acceptance_us,
            "request_timeout_us": limits.request_timeout_us,
        },
        "starvation": {
            "response": "hold_position",
            "grace_us": 1_000_000,
            "require_new_epoch_on_resume": True,
        },
        "runtime": {"device": "cpu" if device == "cpu" else "cuda", "precision": precision},
        "image_encodings": [{"camera_role": i["camera_role"], "encoding": "raw"} for i in images],
    }
    if language:
        spec["language"] = {"max_length_bytes": min(256, int(language[0]["max_length_bytes"]))}
    artifact_name = artifact.get("metadata", {}).get("name", "model")
    robot_name = robot.get("metadata", {}).get("name", "robot")
    document = {
        "api_version": API,
        "kind": "ExecutionProfile",
        "metadata": {
            "name": f"{artifact_name}-{robot_name}"[:63],
            "version": (
                f"{'m' if limits.measured else 'e'}{limits.pass_p99_us // 1000}ms"
                f"-r{limits.refill_threshold_actions}-n{robots}"
            ),
            "description": f"Derived by Neura: {limits.summary()}.",
        },
        "spec": spec,
    }
    return document, limits
