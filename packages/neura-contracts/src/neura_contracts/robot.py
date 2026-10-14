"""Robot profile (design §4.2)."""

from __future__ import annotations

import math
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from neura_contracts.core import (
    API_VERSION,
    ActionSemantics,
    ColorOrder,
    ContractDocument,
    ContractModel,
    FaultResponse,
    FeatureName,
    Frame,
    ImageEncoding,
    Metadata,
    Name,
    PositiveInt,
    Unit,
    Version,
    ensure_unique,
)


class CommandFeature(ContractModel):
    """One element of the command vector the controller accepts, in order."""

    name: FeatureName
    unit: Unit
    frame: Frame
    semantics: ActionSemantics
    min: float
    max: float
    max_rate_per_s: Annotated[float, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def _range(self) -> Self:
        if not (math.isfinite(self.min) and math.isfinite(self.max)) or self.min >= self.max:
            raise ValueError("command range must be finite with min < max")
        return self


class StateFeature(ContractModel):
    name: FeatureName
    unit: Unit
    frame: Frame


class Camera(ContractModel):
    role: FeatureName
    width: PositiveInt
    height: PositiveInt
    color_order: ColorOrder
    encodings: list[ImageEncoding] = Field(min_length=1)
    calibration_revision: Version

    @model_validator(mode="after")
    def _unique_encodings(self) -> Self:
        ensure_unique(self.encodings, "camera encoding")
        return self


class Controller(ContractModel):
    name: Name
    revision: Version


class RobotProfileSpec(ContractModel):
    controller: Controller
    control_period_us: PositiveInt
    command_interface: list[CommandFeature] = Field(min_length=1)
    state_features: list[StateFeature] = Field(default_factory=list)
    cameras: list[Camera] = Field(default_factory=list)
    fault_responses: list[FaultResponse] = Field(
        min_length=1,
        description="Local fault responses this integration has validated on hardware or in "
        "simulation. Only these may be selected by an execution profile.",
    )
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ensure_unique([c.name for c in self.command_interface], "command feature")
        ensure_unique([s.name for s in self.state_features], "state feature")
        ensure_unique([c.role for c in self.cameras], "camera role")
        ensure_unique(self.fault_responses, "fault response")
        return self


class RobotProfile(ContractDocument):
    api_version: Literal["neura.dev/v1alpha1"] = API_VERSION
    kind: Literal["RobotProfile"] = "RobotProfile"
    metadata: Metadata
    spec: RobotProfileSpec
