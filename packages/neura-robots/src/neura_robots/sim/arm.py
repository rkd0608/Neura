"""A simulated robot as a robot integration: what ``neura-arm``'s process, the runtime and the
qualification harness see of a real arm (``So101Arm``), on a MuJoCo scene (``scene.Scene``: the
simulated ALOHA, a Menagerie arm).

The runtime commands it and reads it exactly as it would a real arm. Time is the world's
(``sim.world``): in a ``RealTimeWorld`` the physics advances on its own clock, in a
``LockstepWorld`` one control period per tick of the robot process (``advance``, a tick hook).
Either way it advances with whatever position targets are current, as servos keep tracking
their last goal while the policy computes, while a chunk is late and while the robot holds.

Faults a test can inject: ``camera_fault`` (``"frozen"``: the last frame keeps coming with its
old capture time; ``"unplugged"``: no frame), ``fail_commands`` (the controller refuses
commands) and ``release_authority()`` (the actuator is taken away).
"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from neura_contracts import FaultResponse, RobotProfile
from neura_robots.sim.scene import Scene
from neura_robots.sim.world import LockstepWorld, RealTimeWorld
from neura_robots.so101 import ArmError, Sample
from neura_runtime.interfaces import CommandResult, ImageFrame, SensorSnapshot

CameraFault = Literal["frozen", "unplugged"]


World = LockstepWorld[Any] | RealTimeWorld[Any]


class SimArm:
    """``camera_fps``: in a real-time world, the cameras render on their own thread at this rate,
    as real cameras run beside the control loop, and a capture takes the latest frame. Unset, a
    capture renders its frames (deterministic, for a lockstep world)."""

    def __init__(
        self, world: World | Scene, robot: RobotProfile, *, camera_fps: float | None = None
    ) -> None:
        self.world: World = LockstepWorld(world) if isinstance(world, Scene) else world
        self.camera_fps = camera_fps if isinstance(self.world, RealTimeWorld) else None
        self._camera: threading.Thread | None = None
        self._latest: dict[str, ImageFrame] = {}
        self._closing = threading.Event()
        self.sim = self.world.sim
        self._use(robot)
        self.counters: Counter[str] = Counter()
        self.telemetry: deque[Sample] = deque(maxlen=100_000)
        self.last_command: NDArray[np.float64] | None = None
        self.camera_fault: CameraFault | None = None
        self.fail_commands = False
        self._authority = False
        self._commanded: dict[str, float] | None = None
        self._frames: dict[str, ImageFrame] = {}
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
        if self.camera_fps and self.profile.spec.cameras and self._camera is None:
            self._camera = threading.Thread(target=self._film, name="sim-camera", daemon=True)
            self._camera.start()

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
        joints = self.sim.joints
        if names != joints:
            problems.append(
                f"commands must be the scene's {len(joints)} in order ({', '.join(joints)})"
            )
        else:
            for feature, command in zip(spec.command_interface, self.sim.commands, strict=True):
                if feature.unit != command.unit:
                    problems.append(
                        f"{feature.name}: unit {feature.unit}, the arm uses {command.unit}"
                    )
                elif feature.min < command.low - 1e-5 or feature.max > command.high + 1e-5:
                    problems.append(
                        f"{feature.name}: profile allows [{feature.min:g}, {feature.max:g}], "
                        f"the actuator takes [{command.low:g}, {command.high:g}]"
                    )
        for state in spec.state_features:
            if state.name not in joints:
                problems.append(f"{state.name}: the arm does not report it")
        for camera in spec.cameras:
            if camera.role not in self.sim.cameras:
                served = ", ".join(self.sim.cameras) or "none"
                problems.append(f"camera {camera.role}: the scene serves {served}")
            elif (camera.width, camera.height) != (self.sim.width, self.sim.height):
                problems.append(
                    f"camera {camera.role}: {camera.width}x{camera.height}, the scene renders "
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

    def close(self, *, hold: bool | None = None) -> bool:
        """Hold (unless ``hold`` is False), then let go and stop the world. Returns whether the
        hold took."""
        held = hold is not False and self._authority and self._hold()
        self._authority = False
        self._closing.set()
        if self._camera is not None:
            self._camera.join(timeout=5)
        self.world.close()
        return held

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
        joints = self.sim.joints
        state = {name: float(positions[joints.index(name)]) for name in self.states}
        images: dict[str, ImageFrame] = {}
        for camera in self.profile.spec.cameras:
            role = camera.role
            if self.camera_fault == "unplugged":
                self.counters[f"camera_{role}_no_frame"] += 1
            elif self.camera_fault == "frozen" and role in self._frames:
                images[role] = self._frames[role]
            elif self._camera is not None:
                if role in self._latest:
                    images[role] = self._frames[role] = self._latest[role]
            else:
                images[role] = self._frames[role] = self._frame(role, now_us)
        return SensorSnapshot(state=state, state_time_us=now_us, images=images)

    def _frame(self, role: str, now_us: int) -> ImageFrame:
        image = self.world.render(role)
        return ImageFrame(
            width=image.shape[1],
            height=image.shape[0],
            data=np.ascontiguousarray(image, dtype=np.uint8).tobytes(),
            capture_time_us=now_us,
        )

    def _film(self) -> None:
        """The camera thread: every camera, every 1/fps, stamped when its state was taken."""
        assert self.camera_fps is not None
        period = 1 / self.camera_fps
        try:
            while not self._closing.is_set():
                started = time.monotonic()
                for camera in self.profile.spec.cameras:
                    self._latest[camera.role] = self._frame(camera.role, self._clock())
                self._closing.wait(max(0.0, period - (time.monotonic() - started)))
        finally:
            self.sim.release_renderer()

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

    def _named(self, values: NDArray[np.float64]) -> dict[str, float]:
        return {name: float(v) for name, v in zip(self.sim.joints, values, strict=True)}
