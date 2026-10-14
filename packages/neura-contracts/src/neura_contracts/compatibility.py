"""Compatibility check between a model artifact, a robot profile and an execution profile (§4.4).

The check is a pure function. It collects every issue rather than stopping at the first, so a
user sees the whole list of what to fix. It never converts anything implicitly: a difference is
either covered by an explicit transform in the execution profile or reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from neura_contracts.artifact import ImageInput, LanguageInput, ModelArtifact, StateInput
from neura_contracts.core import ActionSemantics, Frame, ImageEncoding, Unit
from neura_contracts.execution import (
    ConvertUnit,
    ExecutionProfile,
    ReorderActions,
    RtcMerge,
)
from neura_contracts.reason_codes import ReasonCode
from neura_contracts.robot import RobotProfile
from neura_contracts.units import conversion_factor


@dataclass(frozen=True, slots=True)
class CompatibilityIssue:
    code: ReasonCode
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    artifact_digest: str
    robot_profile_digest: str
    execution_profile_digest: str
    issues: tuple[CompatibilityIssue, ...] = field(default_factory=tuple)

    @property
    def compatible(self) -> bool:
        return not self.issues

    def codes(self) -> set[ReasonCode]:
        return {issue.code for issue in self.issues}


@dataclass(frozen=True, slots=True)
class _Action:
    name: str
    unit: Unit
    frame: Frame
    semantics: ActionSemantics


class _Collector:
    def __init__(self) -> None:
        self.issues: list[CompatibilityIssue] = []

    def add(self, code: ReasonCode, path: str, message: str) -> None:
        self.issues.append(CompatibilityIssue(code=code, path=path, message=message))


def check_compatibility(
    artifact: ModelArtifact, robot: RobotProfile, profile: ExecutionProfile
) -> CompatibilityReport:
    out = _Collector()
    artifact_digest = artifact.digest()
    robot_digest = robot.digest()

    _check_binding(artifact_digest, robot_digest, profile, out)
    _check_timing(artifact, robot, profile, out)
    _check_policy(artifact, robot, profile, out)
    _check_actions(artifact, robot, profile, out)
    _check_state(artifact, robot, profile, out)
    _check_cameras(artifact, robot, profile, out)
    _check_language(artifact, profile, out)

    return CompatibilityReport(
        artifact_digest=artifact_digest,
        robot_profile_digest=robot_digest,
        execution_profile_digest=profile.digest(),
        issues=tuple(out.issues),
    )


def _check_binding(
    artifact_digest: str, robot_digest: str, profile: ExecutionProfile, out: _Collector
) -> None:
    binding = profile.spec.binding
    if binding.model_artifact != artifact_digest:
        out.add(
            ReasonCode.COMPAT_ARTIFACT_DIGEST_MISMATCH,
            "spec.binding.model_artifact",
            f"profile is bound to {binding.model_artifact}, artifact is {artifact_digest}",
        )
    if binding.robot_profile != robot_digest:
        out.add(
            ReasonCode.COMPAT_ROBOT_PROFILE_DIGEST_MISMATCH,
            "spec.binding.robot_profile",
            f"profile is bound to {binding.robot_profile}, robot profile is {robot_digest}",
        )


def _check_timing(
    artifact: ModelArtifact, robot: RobotProfile, profile: ExecutionProfile, out: _Collector
) -> None:
    model_period = artifact.spec.outputs.control_period_us
    robot_period = robot.spec.control_period_us
    profile_period = profile.spec.control_period_us
    if not model_period == robot_period == profile_period:
        out.add(
            ReasonCode.COMPAT_CONTROL_PERIOD_MISMATCH,
            "spec.control_period_us",
            f"model {model_period} us, robot {robot_period} us, profile {profile_period} us; "
            "resampling is not supported",
        )
    chunk_length = artifact.spec.outputs.chunk_length
    chunk = profile.spec.chunk
    if chunk.actions_per_chunk > chunk_length:
        out.add(
            ReasonCode.COMPAT_CHUNK_SETTINGS_INVALID,
            "spec.chunk.actions_per_chunk",
            f"actions_per_chunk {chunk.actions_per_chunk} exceeds model chunk length "
            f"{chunk_length}",
        )
    merge = profile.spec.merge
    if isinstance(merge, RtcMerge) and merge.max_conditioned_delay_actions >= chunk_length:
        out.add(
            ReasonCode.COMPAT_CHUNK_SETTINGS_INVALID,
            "spec.merge.max_conditioned_delay_actions",
            "conditioned delay must be shorter than the model chunk length",
        )


def _check_policy(
    artifact: ModelArtifact, robot: RobotProfile, profile: ExecutionProfile, out: _Collector
) -> None:
    spec = artifact.spec
    if spec.properties.stateful:
        out.add(
            ReasonCode.COMPAT_STATEFUL_POLICY_UNSUPPORTED,
            "spec.properties.stateful",
            "stateful policies need isolated session state, which this release does not provide",
        )
    mode = profile.spec.merge.mode
    if mode not in spec.validated_merge_modes:
        validated = ", ".join(m.value for m in spec.validated_merge_modes)
        out.add(
            ReasonCode.COMPAT_MERGE_MODE_NOT_VALIDATED,
            "spec.merge.mode",
            f"merge mode {mode} is not validated for this model (validated: {validated})",
        )
    response = profile.spec.starvation.response
    if response not in robot.spec.fault_responses:
        validated = ", ".join(r.value for r in robot.spec.fault_responses)
        out.add(
            ReasonCode.COMPAT_FAULT_RESPONSE_NOT_VALIDATED,
            "spec.starvation.response",
            f"{response} is not validated for this robot (validated: {validated})",
        )
    selection = profile.spec.runtime
    support = next((d for d in spec.devices if d.device == selection.device), None)
    if support is None or selection.precision not in support.precisions:
        out.add(
            ReasonCode.COMPAT_PRECISION_UNSUPPORTED,
            "spec.runtime",
            f"model does not support {selection.device} at {selection.precision}",
        )


def _check_actions(
    artifact: ModelArtifact, robot: RobotProfile, profile: ExecutionProfile, out: _Collector
) -> None:
    actions = [
        _Action(f.name, f.unit, f.frame, f.semantics) for f in artifact.spec.outputs.features
    ]
    reorders = 0
    converted: set[str] = set()
    for index, transform in enumerate(profile.spec.transforms):
        path = f"spec.transforms[{index}]"
        if isinstance(transform, ReorderActions):
            reorders += 1
            names = [a.name for a in actions]
            if reorders > 1:
                out.add(ReasonCode.COMPAT_TRANSFORM_INVALID, path, "at most one reorder_actions")
            elif sorted(transform.order) != sorted(names):
                out.add(
                    ReasonCode.COMPAT_TRANSFORM_INVALID,
                    path,
                    "reorder_actions.order must be a permutation of the model's action features",
                )
            else:
                by_name = {a.name: a for a in actions}
                actions = [by_name[name] for name in transform.order]
        elif isinstance(transform, ConvertUnit) and transform.target == "action":
            actions = _convert_action(actions, transform, converted, path, out)

    commands = robot.spec.command_interface
    action_names = [a.name for a in actions]
    command_names = [c.name for c in commands]
    for name in action_names:
        if name not in command_names:
            out.add(
                ReasonCode.COMPAT_ACTION_FEATURE_MISSING,
                "spec.outputs.features",
                f"model outputs {name!r}, which the robot does not accept",
            )
    for name in command_names:
        if name not in action_names:
            out.add(
                ReasonCode.COMPAT_ACTION_FEATURE_UNUSED,
                "spec.command_interface",
                f"robot requires {name!r}, which the model does not output",
            )
    if set(action_names) == set(command_names) and action_names != command_names:
        out.add(
            ReasonCode.COMPAT_ACTION_ORDER_MISMATCH,
            "spec.outputs.features",
            f"model order {action_names} differs from robot order {command_names}",
        )

    by_name = {a.name: a for a in actions}
    for command in commands:
        action = by_name.get(command.name)
        if action is None:
            continue
        if action.unit != command.unit:
            out.add(
                ReasonCode.COMPAT_UNIT_MISMATCH,
                f"action:{command.name}",
                f"model {action.unit}, robot {command.unit}",
            )
        if action.frame != command.frame:
            out.add(
                ReasonCode.COMPAT_FRAME_MISMATCH,
                f"action:{command.name}",
                f"model {action.frame}, robot {command.frame}",
            )
        if action.semantics != command.semantics:
            out.add(
                ReasonCode.COMPAT_SEMANTICS_MISMATCH,
                f"action:{command.name}",
                f"model {action.semantics}, robot {command.semantics}",
            )


def _convert_action(
    actions: list[_Action],
    transform: ConvertUnit,
    converted: set[str],
    path: str,
    out: _Collector,
) -> list[_Action]:
    index = next((i for i, a in enumerate(actions) if a.name == transform.feature), None)
    if index is None:
        out.add(
            ReasonCode.COMPAT_TRANSFORM_INVALID,
            path,
            f"model has no action feature {transform.feature!r}",
        )
        return actions
    if transform.feature in converted:
        out.add(ReasonCode.COMPAT_TRANSFORM_INVALID, path, "feature already converted")
        return actions
    action = actions[index]
    if action.unit != transform.from_unit:
        out.add(
            ReasonCode.COMPAT_TRANSFORM_INVALID,
            path,
            f"{transform.feature!r} is in {action.unit}, not {transform.from_unit}",
        )
        return actions
    if conversion_factor(transform.from_unit, transform.to_unit) is None:
        out.add(
            ReasonCode.COMPAT_TRANSFORM_INVALID,
            path,
            f"conversion {transform.from_unit} -> {transform.to_unit} is not supported",
        )
        return actions
    converted.add(transform.feature)
    updated = list(actions)
    updated[index] = _Action(action.name, transform.to_unit, action.frame, action.semantics)
    return updated


def _check_state(
    artifact: ModelArtifact, robot: RobotProfile, profile: ExecutionProfile, out: _Collector
) -> None:
    provided = {s.name: s for s in robot.spec.state_features}
    conversions: dict[str, ConvertUnit] = {}
    for index, transform in enumerate(profile.spec.transforms):
        if isinstance(transform, ConvertUnit) and transform.target == "state":
            path = f"spec.transforms[{index}]"
            feature = provided.get(transform.feature)
            if feature is None:
                out.add(
                    ReasonCode.COMPAT_TRANSFORM_INVALID,
                    path,
                    f"robot has no state feature {transform.feature!r}",
                )
            elif transform.feature in conversions:
                out.add(ReasonCode.COMPAT_TRANSFORM_INVALID, path, "feature already converted")
            elif feature.unit != transform.from_unit:
                out.add(
                    ReasonCode.COMPAT_TRANSFORM_INVALID,
                    path,
                    f"{transform.feature!r} is in {feature.unit}, not {transform.from_unit}",
                )
            elif conversion_factor(transform.from_unit, transform.to_unit) is None:
                out.add(
                    ReasonCode.COMPAT_TRANSFORM_INVALID,
                    path,
                    f"conversion {transform.from_unit} -> {transform.to_unit} is not supported",
                )
            else:
                conversions[transform.feature] = transform

    for model_input in artifact.spec.inputs:
        if not isinstance(model_input, StateInput):
            continue
        for component in model_input.components:
            path = f"input:{model_input.name}.{component.name}"
            feature = provided.get(component.name)
            if feature is None:
                out.add(
                    ReasonCode.COMPAT_STATE_FEATURE_MISSING,
                    path,
                    f"robot does not provide state feature {component.name!r}",
                )
                continue
            conversion = conversions.get(component.name)
            unit = conversion.to_unit if conversion is not None else feature.unit
            if unit != component.unit or feature.frame != component.frame:
                out.add(
                    ReasonCode.COMPAT_STATE_FEATURE_MISMATCH,
                    path,
                    f"model expects {component.unit}/{component.frame}, "
                    f"robot provides {unit}/{feature.frame}",
                )


def _check_cameras(
    artifact: ModelArtifact, robot: RobotProfile, profile: ExecutionProfile, out: _Collector
) -> None:
    cameras = {c.role: c for c in robot.spec.cameras}
    selections = {e.camera_role: e for e in profile.spec.image_encodings}
    used_roles: set[str] = set()
    for model_input in artifact.spec.inputs:
        if not isinstance(model_input, ImageInput):
            continue
        role = model_input.camera_role
        used_roles.add(role)
        path = f"input:{model_input.name}"
        camera = cameras.get(role)
        if camera is None:
            out.add(ReasonCode.COMPAT_CAMERA_MISSING, path, f"robot has no camera {role!r}")
            continue
        if (camera.width, camera.height) != (model_input.width, model_input.height):
            out.add(
                ReasonCode.COMPAT_CAMERA_MISMATCH,
                path,
                f"model expects {model_input.width}x{model_input.height}, "
                f"camera provides {camera.width}x{camera.height}",
            )
        if camera.color_order != model_input.color_order:
            out.add(
                ReasonCode.COMPAT_CAMERA_MISMATCH,
                path,
                f"model expects {model_input.color_order}, camera provides {camera.color_order}",
            )
        selection = selections.get(role)
        if selection is None:
            out.add(
                ReasonCode.COMPAT_IMAGE_ENCODING_UNSUPPORTED,
                "spec.image_encodings",
                f"no encoding selected for camera {role!r}",
            )
            continue
        if selection.encoding not in camera.encodings:
            out.add(
                ReasonCode.COMPAT_IMAGE_ENCODING_UNSUPPORTED,
                "spec.image_encodings",
                f"camera {role!r} cannot produce {selection.encoding}",
            )
        requirement = next(
            (r for r in model_input.accepted_encodings if r.encoding == selection.encoding), None
        )
        if requirement is None:
            out.add(
                ReasonCode.COMPAT_IMAGE_ENCODING_UNSUPPORTED,
                "spec.image_encodings",
                f"model does not accept {selection.encoding} for camera {role!r}",
            )
        elif (
            selection.encoding is ImageEncoding.JPEG
            and requirement.min_quality is not None
            and (selection.quality or 0) < requirement.min_quality
        ):
            out.add(
                ReasonCode.COMPAT_IMAGE_ENCODING_UNSUPPORTED,
                "spec.image_encodings",
                f"jpeg quality {selection.quality} is below the model's minimum "
                f"{requirement.min_quality} for camera {role!r}",
            )
    for role in selections:
        if role not in used_roles:
            out.add(
                ReasonCode.COMPAT_IMAGE_ENCODING_UNSUPPORTED,
                "spec.image_encodings",
                f"encoding selected for camera {role!r}, which the model does not use",
            )


def _check_language(artifact: ModelArtifact, profile: ExecutionProfile, out: _Collector) -> None:
    language_input = next((i for i in artifact.spec.inputs if isinstance(i, LanguageInput)), None)
    settings = profile.spec.language
    if language_input is not None and settings is None:
        out.add(
            ReasonCode.COMPAT_LANGUAGE_INPUT_UNSUPPORTED,
            "spec.language",
            "model requires a language instruction; the profile does not provide one",
        )
    elif language_input is None and settings is not None:
        out.add(
            ReasonCode.COMPAT_LANGUAGE_INPUT_UNSUPPORTED,
            "spec.language",
            "profile provides a language instruction; the model does not accept one",
        )
    elif (
        language_input is not None
        and settings is not None
        and settings.max_length_bytes > language_input.max_length_bytes
    ):
        out.add(
            ReasonCode.COMPAT_LANGUAGE_INPUT_UNSUPPORTED,
            "spec.language.max_length_bytes",
            f"profile allows {settings.max_length_bytes} bytes; "
            f"model accepts at most {language_input.max_length_bytes}",
        )
