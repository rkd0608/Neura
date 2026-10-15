"""The SO-101 arm as a Neura robot integration (design §3, §6.3, §7).

``So101Arm`` is the robot runtime's ``Controller`` and ``SensorSource`` for an SO-101-class arm,
built on LeRobot: its Feetech bus driver, the calibration made with ``lerobot-calibrate``, and its
cameras. What Neura adds is the part that makes the arm safe to serve remotely:

- **One actuator authority.** An exclusive lock on the serial device and an exclusive serial port
  (``neura_robots.lock``). ``has_authority`` fails, and the runtime faults, if either is lost.
- **No jump on connect.** Goals are set to the present positions before torque is enabled, so a
  goal left in the servos by an earlier program is never executed.
- **The contract matches the arm.** Before anything moves, the robot profile's command features,
  units and ranges are checked against the arm's motors and calibration: a profile that allows a
  command outside the calibrated range of a joint is refused.
- **Validated local responses.** ``hold_position`` and ``controlled_stop`` set every goal to the
  present position and confirm it by reading the goals and torque back; the runtime reports a
  stop only when that confirmation succeeds. Torque is never dropped as a stop: an SO-101
  falls without it. ``customer_stop_path`` runs the integrator's own command (a relay that
  cuts servo power, say), when one is configured.
- **Honest timestamps.** Joint state is stamped when it is read; each image with its own capture
  time, from its age, on the robot's monotonic clock.
- **Motion limit per command.** LeRobot's ``max_relative_target`` caps how far any joint moves
  in one command (degrees), as a last guard behind the runtime's range checks.
- **A joint that gives up is a failed command.** Every few commands the torque of every joint is
  read back: a servo whose overload protection dropped torque is not following, so the command
  fails and the runtime faults (CONTROLLER_COMMAND_FAILED) and holds the others.

Telemetry (commanded, sent and measured positions) is kept for ``neura-qualify``.
"""

from __future__ import annotations

import contextlib
import subprocess
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from neura_contracts import FaultResponse, RobotProfile
from neura_robots.cameras import SyntheticCamera, latest_frame
from neura_robots.lock import ActuatorLock
from neura_runtime.interfaces import CommandResult, ImageFrame, SensorSnapshot

POSITION_SUFFIX = ".pos"
RESOLUTION = 4096


class ArmError(RuntimeError):
    """The arm cannot be served: not calibrated, or its profile does not fit it."""


@dataclass(frozen=True, slots=True)
class CameraConfig:
    kind: Literal["opencv", "synthetic"] = "opencv"
    index_or_path: int | str | None = None
    fps: float = 30.0


@dataclass(frozen=True, slots=True)
class So101Config:
    port: str
    # LeRobot's calibration: <calibration_dir>/<calibration_id>.json, from `lerobot-calibrate`.
    calibration_id: str
    calibration_dir: Path | None = None
    cameras: dict[str, CameraConfig] = field(default_factory=dict)  # by camera role
    # Largest move of any joint in one command, in degrees (0-100 for the gripper). None: off.
    max_relative_target: float | None = 10.0
    # On exit, hold the last position with torque on (the arm stays up), or release torque.
    on_exit: Literal["hold", "release"] = "hold"
    # How close the goals read back must be to the present positions to confirm a hold.
    hold_tolerance: float = 0.5
    # The integrator's independent stop path, if any: a command that exits 0 once it stopped.
    stop_command: list[str] | None = None
    lock_dir: Path | None = None
    read_retries: int = 2
    # Read torque back every this many commands (a sync read: about 1 ms on the bus).
    torque_check_every: int = 10


@dataclass(frozen=True, slots=True)
class Sample:
    """One reading for qualification: robot clock, measured and last commanded positions."""

    at_us: int
    measured: dict[str, float]
    commanded: dict[str, float] | None
    torque: bool | None = None


RobotFactory = Callable[[So101Config], Any]
CameraFactory = Callable[[str, CameraConfig, int, int], Any]


def lerobot_robot(config: So101Config) -> Any:
    """LeRobot's SO-101 follower for the bus only; Neura manages cameras itself."""
    from lerobot.robots.so_follower import SOFollower, SOFollowerRobotConfig

    return SOFollower(
        SOFollowerRobotConfig(
            port=config.port,
            id=config.calibration_id,
            calibration_dir=config.calibration_dir,
            max_relative_target=config.max_relative_target,
            disable_torque_on_disconnect=config.on_exit == "release",
            use_degrees=True,
            num_read_retries=config.read_retries,
        )
    )


def lerobot_camera(role: str, config: CameraConfig, width: int, height: int) -> Any:
    if config.kind == "synthetic":
        return SyntheticCamera(width, height, config.fps, seed=hash(role) % 97)
    from lerobot.cameras.opencv import OpenCVCamera, OpenCVCameraConfig

    if config.index_or_path is None:
        raise ArmError(f"camera {role}: set index_or_path (a device index or path)")
    return OpenCVCamera(
        OpenCVCameraConfig(
            index_or_path=config.index_or_path, fps=int(config.fps), width=width, height=height
        )
    )


class So101Arm:
    def __init__(
        self,
        config: So101Config,
        robot: RobotProfile,
        *,
        robot_factory: RobotFactory = lerobot_robot,
        camera_factory: CameraFactory = lerobot_camera,
    ) -> None:
        self.config = config
        self._use(robot)
        self.lock = ActuatorLock(config.port, config.lock_dir)
        self._robot_factory = robot_factory
        self._camera_factory = camera_factory
        self.robot: Any = None
        self.cameras: dict[str, Any] = {}
        self.counters: Counter[str] = Counter()
        self.telemetry: deque[Sample] = deque(maxlen=100_000)
        self._commanded: dict[str, float] | None = None
        self.last_command: NDArray[np.float64] | None = None  # as the runtime gave it
        self._clock: Callable[[], int] = lambda: time.monotonic_ns() // 1000

    def _use(self, robot: RobotProfile) -> None:
        self.profile = robot
        spec = robot.spec
        self.commands = [c.name for c in spec.command_interface]
        self.motors = [name.removesuffix(POSITION_SUFFIX) for name in self.commands]
        self.states = [s.name for s in spec.state_features]
        self.responses = set(spec.fault_responses)

    # --- lifecycle --------------------------------------------------------------------------------

    def connect(self) -> None:
        """Take the actuator, connect the bus without moving anything, check the contract,
        then start the cameras. Raises ArmError (or ActuatorBusyError) if it cannot serve."""
        self.lock.acquire()
        try:
            self.robot = self._robot_factory(self.config)
            bus = self.robot.bus
            bus.connect()
            serial = getattr(getattr(bus, "port_handler", None), "ser", None)
            if serial is not None:
                self.lock.make_port_exclusive(serial.fileno())
            if not self.robot.is_calibrated:
                raise ArmError(
                    f"the arm on {self.config.port} does not match calibration "
                    f"'{self.config.calibration_id}': run lerobot-calibrate --robot.type="
                    f"so101_follower --robot.port={self.config.port} "
                    f"--robot.id={self.config.calibration_id}"
                )
            problems = self.contract_problems()
            if problems:
                raise ArmError(
                    "the robot profile does not fit this arm:\n- " + "\n- ".join(problems)
                )
            # Goals to the present positions before torque comes back on: nothing jumps to a goal
            # an earlier program left in the servos.
            with bus.torque_disabled():
                present = bus.sync_read("Present_Position", num_retry=self.config.read_retries)
                bus.sync_write("Goal_Position", present)
            self.robot.configure()  # LeRobot's motor settings; it re-enables torque
            self._sample(present)
            self._connect_cameras()
        except BaseException:
            self.close(hold=False)
            raise

    def _connect_cameras(self) -> None:
        missing = {c.role for c in self.profile.spec.cameras} - set(self.config.cameras)
        if missing:
            raise ArmError(f"no camera configured for {sorted(missing)}")
        for layout in self.profile.spec.cameras:
            camera_config = self.config.cameras[layout.role]
            camera = self._camera_factory(layout.role, camera_config, layout.width, layout.height)
            camera.connect()
            self.cameras[layout.role] = camera

    def _disconnect_cameras(self) -> None:
        for camera in self.cameras.values():
            with contextlib.suppress(Exception):
                camera.disconnect()
        self.cameras = {}

    def adopt(self, robot: RobotProfile) -> None:
        """Serve a new revision's robot profile without letting go of the arm (at a task
        boundary). Raises ArmError, and keeps the current profile, if it does not fit."""
        problems = self.contract_problems(robot)
        if problems:
            raise ArmError("the robot profile does not fit this arm:\n- " + "\n- ".join(problems))
        layout = [(c.role, c.width, c.height) for c in self.profile.spec.cameras]
        self._use(robot)
        if layout != [(c.role, c.width, c.height) for c in robot.spec.cameras]:
            self._disconnect_cameras()
            self._connect_cameras()

    def revalidate(self) -> list[str]:
        """What stands in the way of serving again, after a fault: the actuator, the bus, the
        calibration and the contract. Empty when a reset may proceed."""
        if not self.has_authority():
            return ["this process no longer holds the actuator: restart it"]
        try:
            if not self.robot.is_calibrated:
                return [f"the arm no longer matches calibration '{self.config.calibration_id}'"]
        except Exception as exc:
            return [f"the bus does not answer: {type(exc).__name__}: {exc}"]
        return self.contract_problems()

    def recover(self) -> None:
        """Before a reset: every goal to the present position, then torque back on any joint
        whose overload protection dropped it, so nothing jumps."""
        bus = self.robot.bus
        present = bus.sync_read("Present_Position", num_retry=self.config.read_retries)
        bus.sync_write("Goal_Position", present)
        off = self._torque_off()
        if off:  # a sync write: no status packet, so a latched overload error cannot refuse it
            bus.sync_write("Torque_Enable", dict.fromkeys(off, 1))
        self._commanded = {f"{k}{POSITION_SUFFIX}": float(v) for k, v in present.items()}

    def contract_problems(self, robot: RobotProfile | None = None) -> list[str]:
        """Where a robot profile (by default the one served) and this arm disagree: names,
        units, ranges, fault responses."""
        from lerobot.motors import MotorNormMode

        profile = robot or self.profile
        bus = self.robot.bus
        problems = []
        for feature in profile.spec.command_interface:
            motor = feature.name.removesuffix(POSITION_SUFFIX)
            if motor not in bus.motors or not feature.name.endswith(POSITION_SUFFIX):
                problems.append(f"{feature.name}: no such joint position on an SO-101")
                continue
            mode = bus.motors[motor].norm_mode
            calibration = bus.calibration[motor]
            if mode is MotorNormMode.DEGREES:
                if feature.unit != "deg":
                    problems.append(f"{feature.name}: unit {feature.unit}, the arm uses deg")
                    continue
                mid = (calibration.range_min + calibration.range_max) / 2
                low = (calibration.range_min - mid) * 360 / (RESOLUTION - 1)
                high = (calibration.range_max - mid) * 360 / (RESOLUTION - 1)
            else:
                if feature.unit != "normalized":
                    problems.append(f"{feature.name}: unit {feature.unit}, the arm uses 0-100")
                    continue
                low, high = 0.0, 100.0
            if feature.min < low - 1e-6 or feature.max > high + 1e-6:
                problems.append(
                    f"{feature.name}: profile allows [{feature.min:g}, {feature.max:g}] but the "
                    f"calibrated range is [{low:.1f}, {high:.1f}]"
                )
        for state in profile.spec.state_features:
            if state.name.removesuffix(POSITION_SUFFIX) not in bus.motors:
                problems.append(f"{state.name}: the arm does not report it")
        unsupported = set(profile.spec.fault_responses) - {
            FaultResponse.HOLD_POSITION,
            FaultResponse.CONTROLLED_STOP,
            *([FaultResponse.CUSTOMER_STOP_PATH] if self.config.stop_command else []),
        }
        if unsupported:
            problems.append(
                f"fault responses {sorted(r.value for r in unsupported)} are not available on "
                "this arm (customer_stop_path needs stop_command)"
            )
        return problems

    def close(self, *, hold: bool | None = None) -> None:
        """Stop the cameras, then hold (default with on_exit=hold) or release, and let go of the
        actuator."""
        hold = self.config.on_exit == "hold" if hold is None else hold
        self._disconnect_cameras()
        if self.robot is not None and getattr(self.robot.bus, "is_connected", False):
            with contextlib.suppress(Exception):
                if hold:
                    self._hold()
                self.robot.bus.disconnect(not hold)
        self.lock.release()

    # --- Controller -------------------------------------------------------------------------------

    def has_authority(self) -> bool:
        return self.lock.held() and bool(self.robot is not None and self.robot.bus.is_connected)

    def command(self, action: NDArray[np.float64]) -> CommandResult:
        self.last_command = action.copy()
        requested = {name: float(value) for name, value in zip(self.commands, action, strict=True)}
        try:
            sent = self.robot.send_action(requested)
        except Exception as exc:  # the bus failed: serial error, timeout, servo error
            self.counters["command_failed"] += 1
            return CommandResult(False, f"{type(exc).__name__}: {exc}")
        if any(abs(sent[k] - v) > 1e-6 for k, v in requested.items() if k in sent):
            self.counters["command_clipped"] += 1  # max_relative_target limited this step
        self._commanded = {k: float(v) for k, v in sent.items()}
        self.counters["commands"] += 1
        if self.counters["commands"] % max(1, self.config.torque_check_every) == 0:
            try:
                off = self._torque_off()
            except Exception as exc:
                self.counters["command_failed"] += 1
                return CommandResult(False, f"{type(exc).__name__}: {exc}")
            if off:
                self.counters["torque_lost"] += 1
                return CommandResult(False, f"torque off on {', '.join(off)} (overload?)")
        return CommandResult(True)

    def apply_response(self, response: FaultResponse) -> bool:
        self.counters[f"response_{response.value}"] += 1
        if response in (FaultResponse.HOLD_POSITION, FaultResponse.CONTROLLED_STOP):
            try:
                return self._hold()
            except Exception:
                return False
        if response is FaultResponse.CUSTOMER_STOP_PATH and self.config.stop_command:
            try:
                done = subprocess.run(  # noqa: S603 - the integrator's own configured command
                    self.config.stop_command, timeout=5, check=False, capture_output=True
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            return done.returncode == 0
        return False  # brake: dropping torque is not a stop on this arm

    def _hold(self) -> bool:
        bus = self.robot.bus
        present = bus.sync_read("Present_Position", num_retry=self.config.read_retries)
        bus.sync_write("Goal_Position", present)
        goals = bus.sync_read("Goal_Position", num_retry=self.config.read_retries)
        self._commanded = {f"{k}{POSITION_SUFFIX}": float(v) for k, v in present.items()}
        held = all(abs(goals[m] - present[m]) <= self.config.hold_tolerance for m in present)
        return held and not self._torque_off()  # a joint without torque is not held

    def _torque_off(self) -> list[str]:
        torque = self.robot.bus.sync_read(
            "Torque_Enable", normalize=False, num_retry=self.config.read_retries
        )
        return [motor for motor, value in torque.items() if not value]

    # --- SensorSource -----------------------------------------------------------------------------

    def capture(self, now_us: int) -> SensorSnapshot | None:
        try:
            present = self.robot.bus.sync_read(
                "Present_Position", num_retry=self.config.read_retries
            )
        except Exception:
            self.counters["state_read_failed"] += 1
            return None
        self._sample(present, now_us)
        state = {name: float(present[name.removesuffix(POSITION_SUFFIX)]) for name in self.states}
        images = {}
        for role, camera in self.cameras.items():
            frame = latest_frame(camera)
            if frame is None:
                self.counters[f"camera_{role}_no_frame"] += 1
                continue
            height, width = frame.image.shape[:2]
            images[role] = ImageFrame(
                width=width,
                height=height,
                data=np.ascontiguousarray(frame.image, dtype=np.uint8).tobytes(),
                capture_time_us=now_us - frame.age_us,
            )
        return SensorSnapshot(state=state, state_time_us=now_us, images=images)

    # --- telemetry --------------------------------------------------------------------------------

    def sample(self, now_us: int | None = None) -> dict[str, float] | None:
        """Read and record the joints (call from the control loop's thread: the bus is not
        shared between threads)."""
        try:
            present = self.robot.bus.sync_read(
                "Present_Position", num_retry=self.config.read_retries
            )
        except Exception:
            self.counters["state_read_failed"] += 1
            return None
        self._sample(present, now_us)
        return {f"{k}{POSITION_SUFFIX}": float(v) for k, v in present.items()}

    def _sample(self, present: dict[str, float], now_us: int | None = None) -> None:
        measured = {f"{k}{POSITION_SUFFIX}": float(v) for k, v in present.items()}
        at = self._clock() if now_us is None else now_us
        self.telemetry.append(Sample(at, measured, dict(self._commanded or {}) or None))
