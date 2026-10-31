"""Shared vocabularies and constrained types for Neura contracts."""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from neura_contracts.canonical import DIGEST_PATTERN, content_digest
from neura_contracts.compat import StrEnum

API_VERSION: Literal["neura.dev/v1alpha1"] = "neura.dev/v1alpha1"


class ContractModel(BaseModel):
    """Base for every contract type: immutable, closed to unknown fields."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        str_strip_whitespace=False,
        allow_inf_nan=False,
    )

    def canonical_data(self) -> dict[str, object]:
        """The JSON data the digest is computed over. Unset optional fields are omitted."""
        return self.model_dump(mode="json", exclude_none=True)


class ContractDocument(ContractModel):
    """Base for top-level documents, which are identified by their content digest."""

    def digest(self) -> str:
        """``sha256:`` digest of the RFC 8785 canonical form of :meth:`canonical_data`."""
        return content_digest(self.canonical_data())


def ensure_unique(items: Iterable[Hashable], what: str) -> None:
    """Raise ValueError naming every duplicated item."""
    seen: set[Hashable] = set()
    dupes: list[str] = []
    for item in items:
        if item in seen and str(item) not in dupes:
            dupes.append(str(item))
        seen.add(item)
    if dupes:
        raise ValueError(f"duplicate {what}: {', '.join(dupes)}")


# --- Constrained scalar types -------------------------------------------------------------------

Digest = Annotated[str, StringConstraints(pattern=DIGEST_PATTERN)]
"""A content digest, ``sha256:`` followed by 64 lowercase hex characters."""

Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")]
"""A resource name: lowercase DNS label."""

FeatureName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
"""A feature, joint or camera identifier, e.g. ``shoulder_pan.pos`` or ``observation.state``."""

Version = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")]
"""A free-form version label, e.g. ``1.4.0`` or ``r3``."""

PositiveInt = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
DurationUs = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
"""A duration in integer microseconds. Integers keep digests exact across languages."""


# --- Vocabularies -------------------------------------------------------------------------------


class Unit(StrEnum):
    RAD = "rad"
    DEG = "deg"
    M = "m"
    MM = "mm"
    RAD_PER_S = "rad_per_s"
    DEG_PER_S = "deg_per_s"
    M_PER_S = "m_per_s"
    NEWTON = "newton"
    NEWTON_METER = "newton_meter"
    NORMALIZED = "normalized"
    """Dimensionless, scaled to a range declared by the integration (for example -1..1)."""
    FRACTION = "fraction"
    """Dimensionless 0..1, for example gripper opening."""
    UNITLESS = "unitless"


class Frame(StrEnum):
    JOINT_SPACE = "joint_space"
    ROBOT_BASE = "robot_base"
    WORLD = "world"
    END_EFFECTOR = "end_effector"
    CAMERA = "camera"


class ActionSemantics(StrEnum):
    ABSOLUTE_JOINT_POSITION = "absolute_joint_position"
    DELTA_JOINT_POSITION = "delta_joint_position"
    JOINT_VELOCITY = "joint_velocity"
    JOINT_TORQUE = "joint_torque"
    EE_POSE_ABSOLUTE = "ee_pose_absolute"
    EE_POSE_DELTA = "ee_pose_delta"
    GRIPPER_POSITION = "gripper_position"


class Dtype(StrEnum):
    FLOAT64 = "float64"
    FLOAT32 = "float32"
    FLOAT16 = "float16"
    BFLOAT16 = "bfloat16"
    INT64 = "int64"
    INT32 = "int32"
    UINT8 = "uint8"
    BOOL = "bool"


class ColorOrder(StrEnum):
    RGB = "rgb"
    BGR = "bgr"


class ImageEncoding(StrEnum):
    RAW = "raw"
    JPEG = "jpeg"
    PNG = "png"


class Precision(StrEnum):
    FP32 = "fp32"
    FP16 = "fp16"
    BF16 = "bf16"
    FP8 = "fp8"
    INT8 = "int8"


class DeviceKind(StrEnum):
    CUDA = "cuda"
    CPU = "cpu"


class MergeMode(StrEnum):
    """How a new action chunk combines with queued future actions (design §5.3)."""

    REPLACE = "replace"
    APPEND = "append"
    BLEND = "blend"
    RTC = "rtc"


class FaultResponse(StrEnum):
    """Local responses a robot integration may have validated (design §4.2, §7)."""

    HOLD_POSITION = "hold_position"
    CONTROLLED_STOP = "controlled_stop"
    BRAKE = "brake"
    CUSTOMER_STOP_PATH = "customer_stop_path"


class Metadata(ContractModel):
    name: Name
    version: Version
    description: str | None = Field(default=None, max_length=2000)
    labels: dict[Name, Annotated[str, StringConstraints(max_length=256)]] = Field(
        default_factory=dict
    )
