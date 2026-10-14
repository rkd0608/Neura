"""Schema-level rules each document enforces on its own, before any compatibility check."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from neura_contracts import ContractError, parse_document

Mutation = Callable[[dict[str, Any]], None]


def _rejects(data: dict[str, Any], mutate: Mutation) -> None:
    mutated = copy.deepcopy(data)
    mutate(mutated)
    with pytest.raises(ContractError):
        parse_document(mutated)


def _set(path: str, value: Any) -> Mutation:
    def mutate(data: dict[str, Any]) -> None:
        target: Any = data
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[int(key)] if isinstance(target, list) else target[key]
        last = keys[-1]
        if isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value

    return mutate


ARTIFACT_REJECTIONS: dict[str, Mutation] = {
    "unpinned runtime image": _set(
        "spec.runtime_image.reference", "registry.example.com/act:latest"
    ),
    "weight path traversal": _set("spec.weights.0.path", "../secrets.bin"),
    "absolute weight path": _set("spec.weights.0.path", "/etc/model.bin"),
    "bad digest": _set("spec.weights.0.digest", "sha256:XYZ"),
    "jpeg without quality": _set("spec.inputs.1.accepted_encodings", [{"encoding": "jpeg"}]),
    "quality on raw": _set(
        "spec.inputs.1.accepted_encodings", [{"encoding": "raw", "min_quality": 90}]
    ),
    "language flag without input": _set("spec.properties.language_conditioned", True),
    "rtc without rtc_capable": _set("spec.validated_merge_modes", ["replace", "rtc"]),
    "duplicate action feature": lambda d: d["spec"]["outputs"]["features"].append(
        copy.deepcopy(d["spec"]["outputs"]["features"][0])
    ),
    "duplicate camera role": _set("spec.inputs.2.camera_role", "front"),
    "zero chunk length": _set("spec.outputs.chunk_length", 0),
    "license without identity": _set("spec.licenses.weights", {"url": "https://example.com"}),
    "uppercase name": _set("metadata.name", "ACT"),
    "unknown unit": _set("spec.outputs.features.0.unit", "furlong"),
}


@pytest.mark.parametrize("mutate", ARTIFACT_REJECTIONS.values(), ids=ARTIFACT_REJECTIONS.keys())
def test_artifact_rejections(artifact_data: dict[str, Any], mutate: Mutation) -> None:
    _rejects(artifact_data, mutate)


ROBOT_REJECTIONS: dict[str, Mutation] = {
    "inverted range": _set("spec.command_interface.0.min", 200.0),
    "empty range": _set("spec.command_interface.0.max", -110.0),
    "duplicate command": lambda d: d["spec"]["command_interface"].append(
        copy.deepcopy(d["spec"]["command_interface"][0])
    ),
    "no fault responses": _set("spec.fault_responses", []),
    "duplicate fault response": _set("spec.fault_responses", ["hold_position", "hold_position"]),
    "camera without encodings": _set("spec.cameras.0.encodings", []),
    "negative rate": _set("spec.command_interface.0.max_rate_per_s", -1.0),
}


@pytest.mark.parametrize("mutate", ROBOT_REJECTIONS.values(), ids=ROBOT_REJECTIONS.keys())
def test_robot_rejections(robot_data: dict[str, Any], mutate: Mutation) -> None:
    _rejects(robot_data, mutate)


PROFILE_REJECTIONS: dict[str, Mutation] = {
    "refill not below chunk": _set("spec.chunk.refill_threshold_actions", 50),
    "min useful above chunk": _set("spec.chunk.min_useful_actions", 51),
    "blend weights length": _set(
        "spec.merge", {"mode": "blend", "window_actions": 3, "new_chunk_weights": [0.5, 1.0]}
    ),
    "blend weight out of range": _set(
        "spec.merge", {"mode": "blend", "window_actions": 1, "new_chunk_weights": [1.5]}
    ),
    "blend window above chunk": _set(
        "spec.merge",
        {"mode": "blend", "window_actions": 60, "new_chunk_weights": [0.5] * 60},
    ),
    "unknown merge mode": _set("spec.merge", {"mode": "interpolate"}),
    "acceptance budget below dispatch": _set(
        "spec.freshness.max_observation_age_at_acceptance_us", 50000
    ),
    "jpeg without quality": _set(
        "spec.image_encodings.0", {"camera_role": "front", "encoding": "jpeg"}
    ),
    "duplicate camera encoding": _set(
        "spec.image_encodings.1", {"camera_role": "front", "encoding": "raw"}
    ),
    "noop unit conversion": _set(
        "spec.transforms",
        [
            {
                "kind": "convert_unit",
                "target": "action",
                "feature": "x",
                "from_unit": "deg",
                "to_unit": "deg",
            }
        ],
    ),
    "duplicate reorder entry": _set(
        "spec.transforms", [{"kind": "reorder_actions", "order": ["a", "a"]}]
    ),
    "negative grace": _set("spec.starvation.grace_us", -1),
}


@pytest.mark.parametrize("mutate", PROFILE_REJECTIONS.values(), ids=PROFILE_REJECTIONS.keys())
def test_profile_rejections(profile_data: dict[str, Any], mutate: Mutation) -> None:
    _rejects(profile_data, mutate)
