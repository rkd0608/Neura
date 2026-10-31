"""Model artifact manifest (design §4.1)."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator
from typing_extensions import Self

from neura_contracts.core import (
    API_VERSION,
    ActionSemantics,
    ColorOrder,
    ContractDocument,
    ContractModel,
    DeviceKind,
    Digest,
    Dtype,
    FeatureName,
    Frame,
    ImageEncoding,
    MergeMode,
    Metadata,
    Name,
    PositiveInt,
    Precision,
    Unit,
    Version,
    ensure_unique,
)

_OCI_PINNED = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
_PATH_CHARS = re.compile(r"^[A-Za-z0-9._/-]{1,512}$")


class WeightFile(ContractModel):
    path: str = Field(description="Relative POSIX path inside the artifact package.")
    digest: Digest
    size_bytes: PositiveInt

    @model_validator(mode="after")
    def _relative(self) -> Self:
        parts = self.path.split("/")
        if (
            not _PATH_CHARS.match(self.path)
            or self.path.startswith("/")
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ValueError("weight path must be a normalized relative path without '..'")
        return self


class RuntimeImage(ContractModel):
    reference: str = Field(
        description="OCI image reference pinned by digest, e.g. registry/neura/act@sha256:<hex>.",
    )

    @model_validator(mode="after")
    def _pinned(self) -> Self:
        if not _OCI_PINNED.match(self.reference):
            raise ValueError("runtime image must be pinned by digest (name@sha256:<64 hex>)")
        return self


class AdapterRef(ContractModel):
    name: Name
    version: Version


class Processing(ContractModel):
    preprocessing_version: Version
    postprocessing_version: Version
    normalization_stats_digest: Digest | None = None


class StateComponent(ContractModel):
    name: FeatureName
    unit: Unit
    frame: Frame


class StateInput(ContractModel):
    kind: Literal["state"] = "state"
    name: FeatureName
    dtype: Dtype = Dtype.FLOAT32
    components: list[StateComponent] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_components(self) -> Self:
        ensure_unique([c.name for c in self.components], "state component")
        return self


class EncodingRequirement(ContractModel):
    encoding: ImageEncoding
    min_quality: Annotated[int, Field(strict=True, ge=1, le=100)] | None = None

    @model_validator(mode="after")
    def _quality_only_for_jpeg(self) -> Self:
        if self.encoding is not ImageEncoding.JPEG and self.min_quality is not None:
            raise ValueError("min_quality applies only to jpeg")
        if self.encoding is ImageEncoding.JPEG and self.min_quality is None:
            raise ValueError("jpeg requires min_quality, because compression changes model inputs")
        return self


class ImageInput(ContractModel):
    kind: Literal["image"] = "image"
    name: FeatureName
    camera_role: FeatureName
    width: PositiveInt
    height: PositiveInt
    color_order: ColorOrder
    accepted_encodings: list[EncodingRequirement] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_encodings(self) -> Self:
        ensure_unique([e.encoding for e in self.accepted_encodings], "accepted encoding")
        return self


class LanguageInput(ContractModel):
    kind: Literal["language"] = "language"
    name: FeatureName
    max_length_bytes: PositiveInt


ModelInput = Annotated[StateInput | ImageInput | LanguageInput, Field(discriminator="kind")]


class ActionFeature(ContractModel):
    name: FeatureName
    unit: Unit
    frame: Frame
    semantics: ActionSemantics


class ActionOutput(ContractModel):
    features: list[ActionFeature] = Field(min_length=1)
    chunk_length: PositiveInt
    control_period_us: PositiveInt
    dtype: Dtype = Dtype.FLOAT32

    @model_validator(mode="after")
    def _unique_features(self) -> Self:
        ensure_unique([f.name for f in self.features], "action feature")
        return self


class PolicyProperties(ContractModel):
    deterministic: bool
    stateful: bool
    language_conditioned: bool
    rtc_capable: bool
    batchable: bool


class DeviceSupport(ContractModel):
    device: DeviceKind
    precisions: list[Precision] = Field(min_length=1)
    min_memory_mib: PositiveInt | None = None

    @model_validator(mode="after")
    def _unique_precisions(self) -> Self:
        ensure_unique(self.precisions, "precision")
        return self


class LicenseRef(ContractModel):
    spdx_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9.+-]{1,64}$")] | None = None
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    url: Annotated[str, StringConstraints(pattern=r"^https://\S{1,2000}$")] | None = None

    @model_validator(mode="after")
    def _identified(self) -> Self:
        if self.spdx_id is None and self.name is None:
            raise ValueError("a license needs an spdx_id or a name")
        return self


class Licenses(ContractModel):
    code: LicenseRef
    weights: LicenseRef


class EvidenceRef(ContractModel):
    kind: Literal["contract_tests", "parity", "closed_loop", "fault_suite", "physical_trial"]
    reference: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    digest: Digest | None = None


class DatasetRef(ContractModel):
    """Recorded episodes a model was trained on (design §28.4): a site's dataset snapshot, named
    by the digest over its episodes. The episodes themselves stay at that site."""

    digest: Digest
    episodes: PositiveInt
    site_id: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None


class TrainingRef(ContractModel):
    """How a model was trained from its base."""

    recipe: dict[FeatureName, str | int | float | bool | None] = Field(default_factory=dict)
    steps: PositiveInt
    # The job that trained it, and the software that ran it.
    job_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    trainer: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class Lineage(ContractModel):
    """Where a fine-tuned model came from: the artifact it started from, the data and the
    training run. Part of the artifact's digest, so it cannot be changed without changing the
    artifact."""

    base_artifact: Digest
    datasets: list[DatasetRef] = Field(min_length=1)
    training: TrainingRef


class ModelArtifactSpec(ContractModel):
    weights: list[WeightFile] = Field(min_length=1)
    runtime_image: RuntimeImage
    adapter: AdapterRef
    processing: Processing
    inputs: list[ModelInput] = Field(min_length=1)
    outputs: ActionOutput
    properties: PolicyProperties
    validated_merge_modes: list[MergeMode] = Field(min_length=1)
    devices: list[DeviceSupport] = Field(min_length=1)
    licenses: Licenses
    provenance: dict[FeatureName, Annotated[str, StringConstraints(max_length=2000)]] = Field(
        default_factory=dict
    )
    evidence: list[EvidenceRef] = Field(default_factory=list)
    lineage: Lineage | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ensure_unique([w.path for w in self.weights], "weight path")
        ensure_unique([i.name for i in self.inputs], "input name")
        ensure_unique(
            [i.camera_role for i in self.inputs if isinstance(i, ImageInput)], "camera role"
        )
        ensure_unique(self.validated_merge_modes, "merge mode")
        ensure_unique([d.device for d in self.devices], "device")
        has_language = any(isinstance(i, LanguageInput) for i in self.inputs)
        if has_language != self.properties.language_conditioned:
            raise ValueError(
                "properties.language_conditioned must be true exactly when a language input exists"
            )
        if MergeMode.RTC in self.validated_merge_modes and not self.properties.rtc_capable:
            raise ValueError("rtc can only be a validated merge mode for an rtc_capable policy")
        return self


class ModelArtifact(ContractDocument):
    api_version: Literal["neura.dev/v1alpha1"] = API_VERSION
    kind: Literal["ModelArtifact"] = "ModelArtifact"
    metadata: Metadata
    spec: ModelArtifactSpec
