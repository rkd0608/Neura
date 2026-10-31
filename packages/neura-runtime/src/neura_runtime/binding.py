"""A verified (artifact, robot profile, execution profile) triple and its explicit transforms.

Construction runs the compatibility check and refuses incompatible triples, so every component that
holds an ``ExecutionBinding`` can rely on the contracts matching. Transforms here are exactly the
ones the execution profile declares (design §4.3); nothing else is ever converted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_contracts import (
    CompatibilityReport,
    ConvertUnit,
    ExecutionProfile,
    ImageInput,
    LanguageInput,
    MergeMode,
    ModelArtifact,
    ReorderActions,
    RobotProfile,
    StateInput,
    check_compatibility,
    conversion_factor,
    load_document,
    parse_document,
)

FloatArray = NDArray[np.float64]


class IncompatibleBindingError(ValueError):
    def __init__(self, report: CompatibilityReport) -> None:
        lines = [f"{i.code} at {i.path}: {i.message}" for i in report.issues]
        super().__init__("incompatible contracts:\n  " + "\n  ".join(lines))
        self.report = report


@dataclass(frozen=True, slots=True)
class StateLayout:
    """How to build one model state tensor from named robot state features."""

    input_name: str
    feature_names: tuple[str, ...]
    factors: FloatArray  # robot units -> model units, per component


@dataclass(frozen=True, slots=True)
class ImageLayout:
    input_name: str
    camera_role: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ActionTransform:
    """Model action space (order, units) to robot command space, and back."""

    robot_from_model: tuple[int, ...]  # robot command i takes model column robot_from_model[i]
    factors: FloatArray  # per robot command: model units -> robot units
    low: FloatArray
    high: FloatArray

    def to_robot(self, model_actions: FloatArray) -> FloatArray:
        return model_actions[:, list(self.robot_from_model)] * self.factors

    def to_model(self, robot_actions: FloatArray) -> FloatArray:
        model = np.empty_like(robot_actions)
        model[:, list(self.robot_from_model)] = robot_actions / self.factors
        return model

    def out_of_range(self, robot_actions: FloatArray) -> list[int]:
        """Row indexes containing any value outside the robot's declared command range (or not
        a number: decoding already refuses those, and this check does not rely on it)."""
        bad = ~((robot_actions >= self.low) & (robot_actions <= self.high))
        return [int(i) for i in np.flatnonzero(bad.any(axis=1))]


class ExecutionBinding:
    def __init__(
        self, artifact: ModelArtifact, robot: RobotProfile, profile: ExecutionProfile
    ) -> None:
        report = check_compatibility(artifact, robot, profile)
        if not report.compatible:
            raise IncompatibleBindingError(report)
        self.artifact = artifact
        self.robot = robot
        self.profile = profile
        self.artifact_digest = report.artifact_digest
        self.robot_profile_digest = report.robot_profile_digest
        self.execution_profile_digest = report.execution_profile_digest

        spec = profile.spec
        self.control_period_us = spec.control_period_us
        self.chunk_length = artifact.spec.outputs.chunk_length
        self.action_dim = len(artifact.spec.outputs.features)
        self.merge_mode = MergeMode(spec.merge.mode)
        self.action_transform = self._action_transform()
        self.state_layouts = self._state_layouts()
        self.image_layouts = tuple(
            ImageLayout(i.name, i.camera_role, i.width, i.height)
            for i in artifact.spec.inputs
            if isinstance(i, ImageInput)
        )
        language = next((i for i in artifact.spec.inputs if isinstance(i, LanguageInput)), None)
        self.language_input = language.name if language is not None else None
        self.language_max_bytes = spec.language.max_length_bytes if spec.language else 0

    def _action_transform(self) -> ActionTransform:
        model_names = [f.name for f in self.artifact.spec.outputs.features]
        order = list(model_names)
        model_factor = dict.fromkeys(model_names, 1.0)
        for transform in self.profile.spec.transforms:
            if isinstance(transform, ReorderActions):
                order = list(transform.order)
            elif isinstance(transform, ConvertUnit) and transform.target == "action":
                factor = conversion_factor(transform.from_unit, transform.to_unit)
                assert factor is not None  # guaranteed by the compatibility check
                model_factor[transform.feature] *= factor
        commands = self.robot.spec.command_interface
        assert [c.name for c in commands] == order
        return ActionTransform(
            robot_from_model=tuple(model_names.index(name) for name in order),
            factors=np.array([model_factor[name] for name in order], dtype=np.float64),
            low=np.array([c.min for c in commands], dtype=np.float64),
            high=np.array([c.max for c in commands], dtype=np.float64),
        )

    def _state_layouts(self) -> tuple[StateLayout, ...]:
        factors: dict[str, float] = {}
        for transform in self.profile.spec.transforms:
            if isinstance(transform, ConvertUnit) and transform.target == "state":
                factor = conversion_factor(transform.from_unit, transform.to_unit)
                assert factor is not None
                factors[transform.feature] = factor
        layouts = []
        for model_input in self.artifact.spec.inputs:
            if isinstance(model_input, StateInput):
                names = tuple(c.name for c in model_input.components)
                layouts.append(
                    StateLayout(
                        input_name=model_input.name,
                        feature_names=names,
                        factors=np.array([factors.get(n, 1.0) for n in names], dtype=np.float64),
                    )
                )
        return tuple(layouts)


def load_binding(artifact: Path, robot: Path, profile: Path) -> ExecutionBinding:
    a = load_document(artifact.read_text(encoding="utf-8"))
    r = load_document(robot.read_text(encoding="utf-8"))
    p = load_document(profile.read_text(encoding="utf-8"))
    assert isinstance(a, ModelArtifact)
    assert isinstance(r, RobotProfile)
    assert isinstance(p, ExecutionProfile)
    return ExecutionBinding(a, r, p)


def binding_from_documents(documents: dict[str, Any]) -> ExecutionBinding:
    """Parse and verify contracts received from the agent; refuses incompatible ones."""
    artifact = parse_document(documents["model_artifact"])
    robot = parse_document(documents["robot_profile"])
    profile = parse_document(documents["execution_profile"])
    if not (
        isinstance(artifact, ModelArtifact)
        and isinstance(robot, RobotProfile)
        and isinstance(profile, ExecutionProfile)
    ):
        raise ValueError("assignment documents have the wrong kinds")
    return ExecutionBinding(artifact, robot, profile)
