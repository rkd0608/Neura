"""Test doubles for integrations: an in-memory controller and sensor source.

Shipped with the package so integrators can unit-test their own code against the runtime.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from neura_contracts import FaultResponse
from neura_runtime.binding import ExecutionBinding
from neura_runtime.interfaces import CommandResult, ImageFrame, SensorSnapshot


class FakeController:
    def __init__(self) -> None:
        self.authority = True
        self.fail = False
        self.confirm = True
        self.commands: list[NDArray[np.float64]] = []
        self.responses: list[FaultResponse] = []

    def has_authority(self) -> bool:
        return self.authority

    def command(self, action: NDArray[np.float64]) -> CommandResult:
        if self.fail:
            return CommandResult(False, "rejected")
        self.commands.append(action.copy())
        return CommandResult(True)

    def apply_response(self, response: FaultResponse) -> bool:
        self.responses.append(response)
        return self.confirm


class FakeSensors:
    def __init__(self, binding: ExecutionBinding) -> None:
        self.names = [s.name for s in binding.robot.spec.state_features]
        self.cameras = {c.role: (c.width, c.height) for c in binding.robot.spec.cameras}
        self.frames = {role: bytes(w * h * 3) for role, (w, h) in self.cameras.items()}
        self.age_us = 0
        self.camera_lag_us = 0
        self.missing: set[str] = set()
        self.unavailable = False

    def capture(self, now_us: int) -> SensorSnapshot | None:
        if self.unavailable:
            return None
        t = now_us - self.age_us
        return SensorSnapshot(
            state=dict.fromkeys(self.names, 1.0),
            state_time_us=t,
            images={
                role: ImageFrame(w, h, self.frames[role], t - self.camera_lag_us)
                for role, (w, h) in self.cameras.items()
                if role not in self.missing
            },
        )
