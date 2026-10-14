"""A simulated position-controlled arm, its controller and sensors, with fault injection."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from neura_contracts import FaultResponse, RobotProfile
from neura_runtime.interfaces import CommandResult, ImageFrame, SensorSnapshot


class SimulatedArm:
    """Joints track the last command with a first-order response each control period."""

    def __init__(self, robot: RobotProfile, *, tracking: float = 0.5) -> None:
        commands = robot.spec.command_interface
        self.names = [c.name for c in commands]
        self.low = np.array([c.min for c in commands], dtype=np.float64)
        self.high = np.array([c.max for c in commands], dtype=np.float64)
        self.position = (self.low + self.high) / 2
        self.target = self.position.copy()
        self.tracking = tracking
        self.holding = False

    def step(self) -> None:
        if not self.holding:
            self.position = self.position + (self.target - self.position) * self.tracking

    def state(self) -> dict[str, float]:
        return {name: float(v) for name, v in zip(self.names, self.position, strict=True)}


@dataclass(slots=True)
class ControllerLog:
    commands: list[NDArray[np.float64]] = field(default_factory=list)
    responses: list[FaultResponse] = field(default_factory=list)


class SimController:
    """Implements ``neura_runtime.interfaces.Controller`` over a ``SimulatedArm``."""

    def __init__(self, arm: SimulatedArm) -> None:
        self.arm = arm
        self.log = ControllerLog()
        self.authority = True
        self.fail_commands = False
        self.confirm_responses = True
        self.validated = {FaultResponse.HOLD_POSITION, FaultResponse.CONTROLLED_STOP}

    def has_authority(self) -> bool:
        return self.authority

    def command(self, action: NDArray[np.float64]) -> CommandResult:
        if self.fail_commands:
            return CommandResult(False, "simulated controller rejected the command")
        if np.any(action < self.arm.low) or np.any(action > self.arm.high):
            # A real controller would refuse too; the runtime must never send one.
            raise AssertionError("runtime dispatched an out-of-range action")
        self.log.commands.append(action.copy())
        self.arm.holding = False
        self.arm.target = action.copy()
        return CommandResult(True)

    def apply_response(self, response: FaultResponse) -> bool:
        self.log.responses.append(response)
        if not self.confirm_responses:
            return False
        if response in (FaultResponse.HOLD_POSITION, FaultResponse.CONTROLLED_STOP):
            self.arm.holding = True
            self.arm.target = self.arm.position.copy()
            return True
        return response in self.validated


class SimSensors:
    """Joint state plus synthetic camera frames, timestamped on the robot clock."""

    def __init__(
        self, arm: SimulatedArm, robot: RobotProfile, *, image_bytes: dict[str, bytes] | None = None
    ) -> None:
        self.arm = arm
        self.cameras = {c.role: (c.width, c.height) for c in robot.spec.cameras}
        self._frames = image_bytes or {
            role: bytes(w * h * 3) for role, (w, h) in self.cameras.items()
        }
        self.missing_cameras: set[str] = set()
        self.camera_lag_us: dict[str, int] = {}
        self.unavailable = False

    def capture(self, now_us: int) -> SensorSnapshot | None:
        if self.unavailable:
            return None
        images = {}
        for role, (width, height) in self.cameras.items():
            if role in self.missing_cameras:
                continue
            images[role] = ImageFrame(
                width=width,
                height=height,
                data=self._frames[role],
                capture_time_us=now_us - self.camera_lag_us.get(role, 0),
            )
        return SensorSnapshot(state=self.arm.state(), state_time_us=now_us, images=images)
