from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from neura_contracts import (
    ExecutionProfile,
    ModelArtifact,
    ReasonCode,
    RobotProfile,
    check_compatibility,
)

Build = Callable[..., tuple[ModelArtifact, RobotProfile, ExecutionProfile]]

Data = dict[str, Any]
Mutation = Callable[[Data, Data, Data], object]


def test_examples_are_compatible(
    build: Build, artifact_data: Data, robot_data: Data, profile_data: Data
) -> None:
    report = check_compatibility(*build(artifact_data, robot_data, profile_data))
    assert report.compatible, report.issues
    assert report.issues == ()


def test_language_example_is_compatible(
    build: Build, language_artifact_data: Data, robot_data: Data, language_profile_data: Data
) -> None:
    report = check_compatibility(*build(language_artifact_data, robot_data, language_profile_data))
    assert report.compatible, report.issues


def test_report_carries_digests(
    build: Build, artifact_data: Data, robot_data: Data, profile_data: Data
) -> None:
    artifact, robot, profile = build(artifact_data, robot_data, profile_data)
    report = check_compatibility(artifact, robot, profile)
    assert report.artifact_digest == artifact.digest()
    assert report.robot_profile_digest == robot.digest()
    assert report.execution_profile_digest == profile.digest()


def test_binding_detects_edited_artifact(
    build: Build, artifact_data: Data, robot_data: Data, profile_data: Data
) -> None:
    _, robot, profile = build(artifact_data, robot_data, profile_data)
    artifact_data["spec"]["adapter"]["version"] = "0.2.0"
    edited, _, _ = build(artifact_data, robot_data, profile_data)
    report = check_compatibility(edited, robot, profile)
    assert report.codes() == {ReasonCode.COMPAT_ARTIFACT_DIGEST_MISMATCH}


def test_binding_detects_edited_robot(
    build: Build, artifact_data: Data, robot_data: Data, profile_data: Data
) -> None:
    artifact, _, profile = build(artifact_data, robot_data, profile_data)
    robot_data["spec"]["controller"]["revision"] = "example-2"
    _, edited, _ = build(artifact_data, robot_data, profile_data)
    report = check_compatibility(artifact, edited, profile)
    assert report.codes() == {ReasonCode.COMPAT_ROBOT_PROFILE_DIGEST_MISMATCH}


def _features(artifact: Data) -> list[Data]:
    features: list[Data] = artifact["spec"]["outputs"]["features"]
    return features


def _commands(robot: Data) -> list[Data]:
    commands: list[Data] = robot["spec"]["command_interface"]
    return commands


def _swap_first_two(items: list[Data]) -> None:
    items[0], items[1] = items[1], items[0]


CASES: dict[str, tuple[Mutation, set[ReasonCode]]] = {
    "model action unknown to robot": (
        lambda a, r, p: _features(a).append(
            {
                "name": "extra.pos",
                "unit": "deg",
                "frame": "joint_space",
                "semantics": "absolute_joint_position",
            }
        ),
        {ReasonCode.COMPAT_ACTION_FEATURE_MISSING},
    ),
    "robot command not produced": (
        lambda a, r, p: _features(a).pop(),
        {ReasonCode.COMPAT_ACTION_FEATURE_UNUSED},
    ),
    "action order differs": (
        lambda a, r, p: _swap_first_two(_features(a)),
        {ReasonCode.COMPAT_ACTION_ORDER_MISMATCH},
    ),
    "action unit differs": (
        lambda a, r, p: _features(a)[0].update(unit="rad"),
        {ReasonCode.COMPAT_UNIT_MISMATCH},
    ),
    "action frame differs": (
        lambda a, r, p: _features(a)[0].update(frame="robot_base"),
        {ReasonCode.COMPAT_FRAME_MISMATCH},
    ),
    "absolute vs delta": (
        lambda a, r, p: _features(a)[0].update(semantics="delta_joint_position"),
        {ReasonCode.COMPAT_SEMANTICS_MISMATCH},
    ),
    "state feature missing": (
        lambda a, r, p: r["spec"]["state_features"].pop(0),
        {ReasonCode.COMPAT_STATE_FEATURE_MISSING},
    ),
    "state unit differs": (
        lambda a, r, p: r["spec"]["state_features"][0].update(unit="rad"),
        {ReasonCode.COMPAT_STATE_FEATURE_MISMATCH},
    ),
    "camera missing": (
        lambda a, r, p: (r["spec"]["cameras"].pop(1), p["spec"]["image_encodings"].pop(1)),
        {ReasonCode.COMPAT_CAMERA_MISSING},
    ),
    "camera resolution differs": (
        lambda a, r, p: r["spec"]["cameras"][0].update(width=1280),
        {ReasonCode.COMPAT_CAMERA_MISMATCH},
    ),
    "camera color order differs": (
        lambda a, r, p: r["spec"]["cameras"][0].update(color_order="bgr"),
        {ReasonCode.COMPAT_CAMERA_MISMATCH},
    ),
    "no encoding selected": (
        lambda a, r, p: p["spec"]["image_encodings"].pop(0),
        {ReasonCode.COMPAT_IMAGE_ENCODING_UNSUPPORTED},
    ),
    "camera cannot produce encoding": (
        lambda a, r, p: (r["spec"]["cameras"][0].update(encodings=["jpeg"]),),
        {ReasonCode.COMPAT_IMAGE_ENCODING_UNSUPPORTED},
    ),
    "jpeg quality below model minimum": (
        lambda a, r, p: p["spec"]["image_encodings"][0].update(encoding="jpeg", quality=70),
        {ReasonCode.COMPAT_IMAGE_ENCODING_UNSUPPORTED},
    ),
    "encoding for unused camera": (
        lambda a, r, p: (
            r["spec"]["cameras"].append(
                {
                    "role": "top",
                    "width": 640,
                    "height": 480,
                    "color_order": "rgb",
                    "encodings": ["raw"],
                    "calibration_revision": "c1",
                }
            ),
            p["spec"]["image_encodings"].append({"camera_role": "top", "encoding": "raw"}),
        ),
        {ReasonCode.COMPAT_IMAGE_ENCODING_UNSUPPORTED},
    ),
    "control period differs": (
        lambda a, r, p: r["spec"].update(control_period_us=20000),
        {ReasonCode.COMPAT_CONTROL_PERIOD_MISMATCH},
    ),
    "chunk longer than model": (
        lambda a, r, p: a["spec"]["outputs"].update(chunk_length=40),
        {ReasonCode.COMPAT_CHUNK_SETTINGS_INVALID},
    ),
    "merge mode not validated": (
        lambda a, r, p: p["spec"].update(merge={"mode": "append"}),
        {ReasonCode.COMPAT_MERGE_MODE_NOT_VALIDATED},
    ),
    "starvation response not validated": (
        lambda a, r, p: p["spec"]["starvation"].update(response="brake"),
        {ReasonCode.COMPAT_FAULT_RESPONSE_NOT_VALIDATED},
    ),
    "precision unsupported": (
        lambda a, r, p: p["spec"]["runtime"].update(precision="int8"),
        {ReasonCode.COMPAT_PRECISION_UNSUPPORTED},
    ),
    "device unsupported": (
        lambda a, r, p: p["spec"]["runtime"].update(device="cpu"),
        {ReasonCode.COMPAT_PRECISION_UNSUPPORTED},
    ),
    "stateful policy": (
        lambda a, r, p: a["spec"]["properties"].update(stateful=True),
        {ReasonCode.COMPAT_STATEFUL_POLICY_UNSUPPORTED},
    ),
    "profile gives language to non-language model": (
        lambda a, r, p: p["spec"].update(language={"max_length_bytes": 64}),
        {ReasonCode.COMPAT_LANGUAGE_INPUT_UNSUPPORTED},
    ),
}


@pytest.mark.parametrize(("mutate", "expected"), CASES.values(), ids=CASES.keys())
def test_each_incompatibility_is_reported(
    build: Build,
    artifact_data: Data,
    robot_data: Data,
    profile_data: Data,
    mutate: Mutation,
    expected: set[ReasonCode],
) -> None:
    mutate(artifact_data, robot_data, profile_data)
    report = check_compatibility(*build(artifact_data, robot_data, profile_data))
    assert not report.compatible
    assert report.codes() == expected, report.issues


def test_language_model_without_language_settings(
    build: Build, language_artifact_data: Data, robot_data: Data, language_profile_data: Data
) -> None:
    del language_profile_data["spec"]["language"]
    report = check_compatibility(*build(language_artifact_data, robot_data, language_profile_data))
    assert report.codes() == {ReasonCode.COMPAT_LANGUAGE_INPUT_UNSUPPORTED}


def test_language_length_above_model_limit(
    build: Build, language_artifact_data: Data, robot_data: Data, language_profile_data: Data
) -> None:
    language_profile_data["spec"]["language"]["max_length_bytes"] = 1024
    report = check_compatibility(*build(language_artifact_data, robot_data, language_profile_data))
    assert report.codes() == {ReasonCode.COMPAT_LANGUAGE_INPUT_UNSUPPORTED}


def test_rtc_delay_must_fit_chunk(
    build: Build, artifact_data: Data, robot_data: Data, profile_data: Data
) -> None:
    artifact_data["spec"]["properties"]["rtc_capable"] = True
    artifact_data["spec"]["validated_merge_modes"] = ["replace", "rtc"]
    profile_data["spec"]["merge"] = {"mode": "rtc", "max_conditioned_delay_actions": 100}
    report = check_compatibility(*build(artifact_data, robot_data, profile_data))
    assert report.codes() == {ReasonCode.COMPAT_CHUNK_SETTINGS_INVALID}


def test_all_issues_are_collected(
    build: Build, artifact_data: Data, robot_data: Data, profile_data: Data
) -> None:
    _features(artifact_data)[0]["unit"] = "rad"
    _features(artifact_data)[1]["frame"] = "world"
    robot_data["spec"]["control_period_us"] = 20000
    profile_data["spec"]["runtime"]["precision"] = "int8"
    report = check_compatibility(*build(artifact_data, robot_data, profile_data))
    assert report.codes() == {
        ReasonCode.COMPAT_UNIT_MISMATCH,
        ReasonCode.COMPAT_FRAME_MISMATCH,
        ReasonCode.COMPAT_CONTROL_PERIOD_MISMATCH,
        ReasonCode.COMPAT_PRECISION_UNSUPPORTED,
    }


# --- Explicit transforms ------------------------------------------------------------------------


def test_reorder_transform_resolves_order_mismatch(
    build: Build, artifact_data: Data, robot_data: Data, profile_data: Data
) -> None:
    _swap_first_two(_features(artifact_data))
    profile_data["spec"]["transforms"] = [
        {"kind": "reorder_actions", "order": [c["name"] for c in _commands(robot_data)]}
    ]
    report = check_compatibility(*build(artifact_data, robot_data, profile_data))
    assert report.compatible, report.issues


def test_unit_conversion_resolves_action_unit_mismatch(
    build: Build, artifact_data: Data, robot_data: Data, profile_data: Data
) -> None:
    _features(artifact_data)[0]["unit"] = "rad"
    profile_data["spec"]["transforms"] = [
        {
            "kind": "convert_unit",
            "target": "action",
            "feature": "shoulder_pan.pos",
            "from_unit": "rad",
            "to_unit": "deg",
        }
    ]
    report = check_compatibility(*build(artifact_data, robot_data, profile_data))
    assert report.compatible, report.issues


def test_unit_conversion_resolves_state_unit_mismatch(
    build: Build, artifact_data: Data, robot_data: Data, profile_data: Data
) -> None:
    artifact_data["spec"]["inputs"][0]["components"][0]["unit"] = "rad"
    profile_data["spec"]["transforms"] = [
        {
            "kind": "convert_unit",
            "target": "state",
            "feature": "shoulder_pan.pos",
            "from_unit": "deg",
            "to_unit": "rad",
        }
    ]
    report = check_compatibility(*build(artifact_data, robot_data, profile_data))
    assert report.compatible, report.issues


INVALID_TRANSFORMS: dict[str, list[Data]] = {
    "reorder not a permutation": [{"kind": "reorder_actions", "order": ["shoulder_pan.pos"]}],
    "two reorders": [
        {
            "kind": "reorder_actions",
            "order": [
                "shoulder_pan.pos",
                "shoulder_lift.pos",
                "elbow_flex.pos",
                "wrist_flex.pos",
                "wrist_roll.pos",
                "gripper.pos",
            ],
        },
        {
            "kind": "reorder_actions",
            "order": [
                "shoulder_pan.pos",
                "shoulder_lift.pos",
                "elbow_flex.pos",
                "wrist_flex.pos",
                "wrist_roll.pos",
                "gripper.pos",
            ],
        },
    ],
    "convert unknown action": [
        {
            "kind": "convert_unit",
            "target": "action",
            "feature": "nope",
            "from_unit": "deg",
            "to_unit": "rad",
        }
    ],
    "convert wrong source unit": [
        {
            "kind": "convert_unit",
            "target": "action",
            "feature": "shoulder_pan.pos",
            "from_unit": "rad",
            "to_unit": "deg",
        }
    ],
    "convert across dimensions": [
        {
            "kind": "convert_unit",
            "target": "action",
            "feature": "shoulder_pan.pos",
            "from_unit": "deg",
            "to_unit": "m",
        }
    ],
    "convert same action twice": [
        {
            "kind": "convert_unit",
            "target": "action",
            "feature": "shoulder_pan.pos",
            "from_unit": "deg",
            "to_unit": "rad",
        },
        {
            "kind": "convert_unit",
            "target": "action",
            "feature": "shoulder_pan.pos",
            "from_unit": "rad",
            "to_unit": "deg",
        },
    ],
    "convert unknown state": [
        {
            "kind": "convert_unit",
            "target": "state",
            "feature": "nope",
            "from_unit": "deg",
            "to_unit": "rad",
        }
    ],
    "convert state wrong source unit": [
        {
            "kind": "convert_unit",
            "target": "state",
            "feature": "shoulder_pan.pos",
            "from_unit": "rad",
            "to_unit": "deg",
        }
    ],
    "convert state across dimensions": [
        {
            "kind": "convert_unit",
            "target": "state",
            "feature": "shoulder_pan.pos",
            "from_unit": "deg",
            "to_unit": "mm",
        }
    ],
}


@pytest.mark.parametrize("transforms", INVALID_TRANSFORMS.values(), ids=INVALID_TRANSFORMS.keys())
def test_invalid_transforms_are_reported(
    build: Build, artifact_data: Data, robot_data: Data, profile_data: Data, transforms: list[Data]
) -> None:
    profile_data["spec"]["transforms"] = copy.deepcopy(transforms)
    report = check_compatibility(*build(artifact_data, robot_data, profile_data))
    assert ReasonCode.COMPAT_TRANSFORM_INVALID in report.codes(), report.issues


def test_semantics_mismatch_cannot_be_hidden_by_unit_conversion(
    build: Build, artifact_data: Data, robot_data: Data, profile_data: Data
) -> None:
    _features(artifact_data)[0].update(unit="rad", semantics="delta_joint_position")
    profile_data["spec"]["transforms"] = [
        {
            "kind": "convert_unit",
            "target": "action",
            "feature": "shoulder_pan.pos",
            "from_unit": "rad",
            "to_unit": "deg",
        }
    ]
    report = check_compatibility(*build(artifact_data, robot_data, profile_data))
    assert report.codes() == {ReasonCode.COMPAT_SEMANTICS_MISMATCH}
