"""Execution profile (design §4.3): one model artifact bound to one robot profile."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator
from typing_extensions import Self

from neura_contracts.core import (
    API_VERSION,
    ContractDocument,
    ContractModel,
    DeviceKind,
    Digest,
    DurationUs,
    FaultResponse,
    FeatureName,
    ImageEncoding,
    Metadata,
    NonNegativeInt,
    PositiveInt,
    Precision,
    Unit,
    ensure_unique,
)


class Binding(ContractModel):
    model_artifact: Digest
    robot_profile: Digest


class ChunkSettings(ContractModel):
    actions_per_chunk: PositiveInt = Field(
        description="Actions executed from each chunk; at most the model's chunk length."
    )
    refill_threshold_actions: NonNegativeInt = Field(
        description="Request a new chunk when this many or fewer actions remain queued."
    )
    min_useful_actions: PositiveInt = Field(
        description="Reject a chunk if fewer actions than this remain after trimming (§5.2)."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.refill_threshold_actions >= self.actions_per_chunk:
            raise ValueError("refill_threshold_actions must be less than actions_per_chunk")
        if self.min_useful_actions > self.actions_per_chunk:
            raise ValueError("min_useful_actions cannot exceed actions_per_chunk")
        return self


class ReplaceMerge(ContractModel):
    mode: Literal["replace"] = "replace"


class AppendMerge(ContractModel):
    mode: Literal["append"] = "append"


class BlendMerge(ContractModel):
    mode: Literal["blend"] = "blend"
    window_actions: PositiveInt
    new_chunk_weights: list[Annotated[float, Field(ge=0.0, le=1.0)]] = Field(
        min_length=1,
        description="Weight of the new chunk at each index of the overlap window; the old chunk "
        "gets 1 - weight. After the window the new chunk is used alone.",
    )

    @model_validator(mode="after")
    def _window(self) -> Self:
        if len(self.new_chunk_weights) != self.window_actions:
            raise ValueError("new_chunk_weights must have exactly window_actions entries")
        return self


class RtcMerge(ContractModel):
    mode: Literal["rtc"] = "rtc"
    max_conditioned_delay_actions: PositiveInt = Field(
        description="Largest inference delay, in actions, the model was conditioned on."
    )


MergeConfig = Annotated[
    ReplaceMerge | AppendMerge | BlendMerge | RtcMerge, Field(discriminator="mode")
]


class FreshnessBudget(ContractModel):
    max_observation_age_at_dispatch_us: DurationUs
    max_observation_age_at_acceptance_us: DurationUs
    request_timeout_us: DurationUs

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_observation_age_at_acceptance_us < self.max_observation_age_at_dispatch_us:
            raise ValueError(
                "acceptance happens after dispatch, so its age budget cannot be smaller"
            )
        return self


class StarvationPolicy(ContractModel):
    response: FaultResponse
    grace_us: NonNegativeInt
    require_new_epoch_on_resume: bool = True


class RuntimeSelection(ContractModel):
    device: DeviceKind
    precision: Precision


class CameraEncoding(ContractModel):
    camera_role: FeatureName
    encoding: ImageEncoding
    quality: Annotated[int, Field(strict=True, ge=1, le=100)] | None = None

    @model_validator(mode="after")
    def _quality(self) -> Self:
        if (self.encoding is ImageEncoding.JPEG) != (self.quality is not None):
            raise ValueError("quality is required for jpeg and not allowed otherwise")
        return self


class LanguageSettings(ContractModel):
    max_length_bytes: PositiveInt


class ReorderActions(ContractModel):
    """Robot command index ``i`` receives the model action named ``order[i]``."""

    kind: Literal["reorder_actions"] = "reorder_actions"
    order: list[FeatureName] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ensure_unique(self.order, "feature in reorder")
        return self


class ConvertUnit(ContractModel):
    """Linear unit conversion of one action (model to robot) or state (robot to model) feature."""

    kind: Literal["convert_unit"] = "convert_unit"
    target: Literal["action", "state"]
    feature: FeatureName
    from_unit: Unit
    to_unit: Unit

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.from_unit == self.to_unit:
            raise ValueError("from_unit and to_unit must differ")
        return self


Transform = Annotated[ReorderActions | ConvertUnit, Field(discriminator="kind")]


class ExecutionProfileSpec(ContractModel):
    binding: Binding
    control_period_us: PositiveInt
    chunk: ChunkSettings
    merge: MergeConfig
    freshness: FreshnessBudget
    starvation: StarvationPolicy
    runtime: RuntimeSelection
    image_encodings: list[CameraEncoding] = Field(default_factory=list)
    language: LanguageSettings | None = None
    transforms: list[Transform] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ensure_unique([e.camera_role for e in self.image_encodings], "camera encoding")
        if (
            isinstance(self.merge, BlendMerge)
            and self.merge.window_actions > self.chunk.actions_per_chunk
        ):
            raise ValueError("blend window cannot exceed actions_per_chunk")
        return self


class ExecutionProfile(ContractDocument):
    api_version: Literal["neura.dev/v1alpha1"] = API_VERSION
    kind: Literal["ExecutionProfile"] = "ExecutionProfile"
    metadata: Metadata
    spec: ExecutionProfileSpec
