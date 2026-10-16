"""The simulated ALOHA as a robot integration: what ``neura-arm``'s process, the runtime and the
qualification harness see of a real arm (``So101Arm``), on the MuJoCo scene.

The runtime commands it and reads it exactly as it would a real arm. Time is the world's
(``aloha.world``): in a ``RealTimeWorld`` the physics advances on its own clock, in a
``LockstepWorld`` one control period per tick of the robot process (``advance``, a tick hook).
Either way it advances with whatever position targets are current, as servos keep tracking
their last goal while the policy computes, while a chunk is late and while the robot holds.

Faults a test can inject: ``camera_fault`` (``"frozen"``: the last frame keeps coming with its
old capture time; ``"unplugged"``: no frame), ``fail_commands`` (the controller refuses
commands) and ``release_authority()`` (the actuator is taken away).
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Callable
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from neura_contracts import FaultResponse, RobotProfile
from neura_robots.aloha import JOINTS, AlohaSim
from neura_robots.aloha.contracts import command_range
from neura_robots.aloha.world import LockstepWorld, RealTimeWorld
from neura_robots.so101 import ArmError, Sample
from neura_runtime.interfaces import CommandResult, ImageFrame, SensorSnapshot

CameraFault = Literal["frozen", "unplugged"]


World = LockstepWorld | RealTimeWorld


class AlohaSimArm:
    def __init__(self, world: World | AlohaSim, robot: RobotProfile) -> None:
        self.world: World = LockstepWorld(world) if isinstance(world, AlohaSim) else world
        self.sim = self.world.sim
        self._use(robot)
        self.counters: Counter[str] = Counter()
        self.telemetry: deque[Sample] = deque(maxlen=100_000)
        self.last_command: NDArray[np.float64] | None = None
        self.camera_fault: CameraFault | None = None
        self.fail_commands = False
        self._authority = False
        self._commanded: dict[str, float] | None = None
        self._frame: ImageFrame | None = None
        self._clock: Callable[[], int] = lambda: time.monotonic_ns() // 1000

    def _use(self, robot: RobotProfile) -> None:
        self.profile = robot
        self.commands = [c.name for c in robot.spec.command_interface]
        self.states = [s.name for s in robot.spec.state_features]

    # --- lifecycle -------------------------------------------------------------------------------

    def connect(self) -> None:
        problems = self.contract_problems()
        if problems:
            raise ArmError("the robot profile does not fit this arm:\n- " + "\n- ".join(problems))
        self._authority = True
        self._commanded = self._named(self.world.targets())
        self.sample()

    def adopt(self, robot: RobotProfile) -> None:
        problems = self.contract_problems(robot)
        if problems:
            raise ArmError("the robot profile does not fit this arm:\n- " + "\n- ".join(problems))
        self._use(robot)

    def revalidate(self) -> list[str]:
        if not self._authority:
            return ["this process no longer holds the actuator: restart it"]
        return self.contract_problems()

    def recover(self) -> None:
        """Before a reset: every goal to the present position, so nothing jumps."""
        self._commanded = self._named(self.world.hold())

    def contract_problems(self, robot: RobotProfile | None = None) -> list[str]:
        spec = (robot or self.profile).spec
        problems = []
        names = [c.name for c in spec.command_interface]
        if names != JOINTS:
            problems.append(f"commands must be the 14 ALOHA joints in order ({', '.join(JOINTS)})")
        else:
            for index, feature in enumerate(spec.command_interface):
                unit, low, high = command_range(self.sim, index)
                if feature.unit != unit:
                    problems.append(f"{feature.name}: unit {feature.unit}, the arm uses {unit}")
                elif feature.min < low - 1e-5 or feature.max > high + 1e-5:
                    problems.append(
                        f"{feature.name}: profile allows [{feature.min:g}, {feature.max:g}], "
                        f"the actuator takes [{low:g}, {high:g}]"
                    )
        for state in spec.state_features:
            if state.name not in JOINTS:
                problems.append(f"{state.name}: the arm does not report it")
        for camera in spec.cameras:
            if camera.role != "top":
                problems.append(f"camera {camera.role}: the simulated ALOHA serves the top camera")
            elif (camera.width, camera.height) != (self.sim.width, self.sim.height):
                problems.append(
                    f"camera top: {camera.width}x{camera.height}, the scene renders "
                    f"{self.sim.width}x{self.sim.height}"
                )
        unsupported = set(spec.fault_responses) - {
            FaultResponse.HOLD_POSITION,
            FaultResponse.CONTROLLED_STOP,
        }
        if unsupported:
            problems.append(f"fault responses {sorted(r.value for r in unsupported)} unavailable")
        return problems

    def release_authority(self) -> None:
        """The actuator is taken away (a test's lost-lock fault)."""
        self._authority = False

    def close(self, *, hold: bool | None = None) -> None:
        if hold is not False and self._authority:
            self._hold()
        self._authority = False
        self.world.close()

    # --- Controller ------------------------------------------------------------------------------

    def has_authority(self) -> bool:
        return self._authority

    def command(self, action: NDArray[np.float64]) -> CommandResult:
        self.last_command = action.copy()
        if self.fail_commands:
            self.counters["command_failed"] += 1
            return CommandResult(False, "injected controller failure")
        if not self._authority:
            self.counters["command_failed"] += 1
            return CommandResult(False, "no authority over the actuators")
        self.world.set_targets(action)
        self._commanded = self._named(self.world.targets())
        self.counters["commands"] += 1
        return CommandResult(True)

    def apply_response(self, response: FaultResponse) -> bool:
        self.counters[f"response_{response.value}"] += 1
        if response in (FaultResponse.HOLD_POSITION, FaultResponse.CONTROLLED_STOP):
            return self._hold()
        return False

    def _hold(self) -> bool:
        if not self._authority:
            return False
        present = self.world.hold()
        targets = self.world.targets()
        self._commanded = self._named(targets)
        return bool(np.allclose(targets, present))

    # --- SensorSource ----------------------------------------------------------------------------

    def capture(self, now_us: int) -> SensorSnapshot:
        positions = self.world.qpos()
        self._sample(positions, now_us)
        state = {name: float(positions[JOINTS.index(name)]) for name in self.states}
        images: dict[str, ImageFrame] = {}
        if any(c.role == "top" for c in self.profile.spec.cameras):
            if self.camera_fault == "unplugged":
                self.counters["camera_top_no_frame"] += 1
            elif self.camera_fault == "frozen" and self._frame is not None:
                images["top"] = self._frame
            else:
                image = self.world.render("top")
                self._frame = ImageFrame(
                    width=image.shape[1],
                    height=image.shape[0],
                    data=np.ascontiguousarray(image, dtype=np.uint8).tobytes(),
                    capture_time_us=now_us,
                )
                images["top"] = self._frame
        return SensorSnapshot(state=state, state_time_us=now_us, images=images)

    # --- the physics and telemetry ---------------------------------------------------------------

    def advance(self, now_us: int | None = None) -> None:
        """The robot process's tick hook: one control period of physics in a lockstep world;
        nothing in a real-time one."""
        self.world.advance()

    def sample(self, now_us: int | None = None) -> dict[str, float]:
        positions = self.world.qpos()
        self._sample(positions, now_us)
        return self._named(positions)

    def _sample(self, positions: NDArray[np.float64], now_us: int | None) -> None:
        at = self._clock() if now_us is None else now_us
        self.telemetry.append(Sample(at, self._named(positions), dict(self._commanded or {})))

    @staticmethod
    def _named(values: NDArray[np.float64]) -> dict[str, float]:
        return {name: float(v) for name, v in zip(JOINTS, values, strict=True)}
