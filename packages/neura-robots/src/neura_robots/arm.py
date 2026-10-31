"""What the robot-side process (``process.ArmProcess``) needs of an arm integration: the
runtime's Controller and SensorSource, plus recovery, revalidation and telemetry.
``so101.So101Arm`` is the real SO-101; ``sim.arm.SimArm`` the simulated ALOHA."""

from __future__ import annotations

from collections import Counter, deque
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from neura_contracts import FaultResponse, RobotProfile
from neura_robots.so101 import Sample
from neura_runtime.interfaces import CommandResult, SensorSnapshot


class Arm(Protocol):
    counters: Counter[str]
    telemetry: deque[Sample]
    last_command: NDArray[np.float64] | None

    def has_authority(self) -> bool: ...
    def command(self, action: NDArray[np.float64]) -> CommandResult: ...
    def apply_response(self, response: FaultResponse) -> bool: ...
    def capture(self, now_us: int) -> SensorSnapshot | None: ...
    def sample(self, now_us: int | None = None) -> dict[str, float] | None: ...
    def recover(self) -> None: ...
    def revalidate(self) -> list[str]: ...
    def adopt(self, robot: RobotProfile) -> None: ...
