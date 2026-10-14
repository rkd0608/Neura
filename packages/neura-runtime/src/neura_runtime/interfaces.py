"""Integration points a supported robot integration implements (design §3, §7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from neura_contracts import FaultResponse


@dataclass(frozen=True, slots=True)
class CommandResult:
    ok: bool
    detail: str | None = None


class Controller(Protocol):
    """The customer's existing controller, as seen by the runtime.

    Implementations must be fast and non-blocking: ``command`` is called once per control period.
    """

    def has_authority(self) -> bool:
        """True while this runtime holds exclusive control of the actuators (design §6.3)."""
        ...

    def command(self, action: NDArray[np.float64]) -> CommandResult:
        """Apply one command vector, in robot command order and units."""
        ...

    def apply_response(self, response: FaultResponse) -> bool:
        """Invoke a validated local response. Return True only when the controller confirms it."""
        ...


@dataclass(frozen=True, slots=True)
class ImageFrame:
    width: int
    height: int
    data: bytes  # raw 8-bit, row-major, 3 channels in the camera's declared color order
    capture_time_us: int


@dataclass(frozen=True, slots=True)
class SensorSnapshot:
    """One coherent set of sensor readings, timestamped on the robot's monotonic clock."""

    state: dict[str, float]
    state_time_us: int
    images: dict[str, ImageFrame] = field(default_factory=dict)


class SensorSource(Protocol):
    def capture(self, now_us: int) -> SensorSnapshot | None:
        """Latest complete readings, or None if none are available."""
        ...
