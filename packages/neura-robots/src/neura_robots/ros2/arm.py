"""A ROS 2 robot as an arm Neura's robot-side process drives (``arm.Arm``), through the relay
node running in the robot's ROS environment (``ros2.relay``).

Joints are mapped from ROS names and units to the robot profile's: ``profile = ros * scale +
offset`` (radians to degrees is ``scale: 57.29577951308232``), and back for commands. The runtime
stays the execution authority: it checks every action before ``command`` sends it. This arm has
authority only while the relay is connected and its joint states are fresh; holding position
commands the positions last read, which a ROS position controller keeps. Images arrive as
``rgb8`` or ``bgr8`` and are delivered in the color order the robot profile declares.
"""

from __future__ import annotations

import logging
import math
import socket
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from neura_contracts import FaultResponse, RobotProfile
from neura_robots.ros2.relay import read_frame, send_frame
from neura_robots.so101 import ArmError, Sample
from neura_runtime.interfaces import CommandResult, ImageFrame, SensorSnapshot

log = logging.getLogger("neura.ros2")


@dataclass(frozen=True, slots=True)
class Ros2Joint:
    ros: str  # the joint's name in the robot's JointState
    name: str  # the robot profile's state and command name
    scale: float = 1.0
    offset: float = 0.0


@dataclass(frozen=True, slots=True)
class Ros2Config:
    socket: Path  # the relay's Unix socket
    joints: list[Ros2Joint]
    cameras: list[str] = field(default_factory=list)  # camera roles the relay sends
    # Joint states older than this mean the robot cannot be driven: no authority, no hold.
    state_timeout_ms: float = 100.0
    # How long connect waits for the first joint states and a frame from every camera.
    connect_timeout_s: float = 5.0


class Ros2Arm:
    def __init__(self, config: Ros2Config, robot: RobotProfile) -> None:
        self.config = config
        self.counters: Counter[str] = Counter()
        self.telemetry: deque[Sample] = deque(maxlen=4096)
        self.last_command: NDArray[np.float64] | None = None
        self._robot = robot
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._state: dict[str, float] = {}
        self._state_us = 0
        self._images: dict[str, ImageFrame] = {}
        self._commanded: dict[str, float] | None = None
        self._observed = threading.Event()  # joint states and every camera have arrived

    @property
    def commands(self) -> list[str]:
        return [c.name for c in self._robot.spec.command_interface]

    # --- connection

    def connect(self) -> None:
        """Connect to the relay and wait for the robot's first joint states and camera frames.
        Raises ArmError if the relay is not running or the robot sends nothing in time."""
        self._open()
        if not self._observed.wait(self.config.connect_timeout_s):
            self.close()
            raise ArmError(
                f"the ROS 2 relay on {self.config.socket} sent no joint states and camera frames"
                f" within {self.config.connect_timeout_s:g} s: is the robot's driver running?"
            )

    def _open(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self.config.socket))
        except OSError as exc:
            sock.close()
            raise ArmError(f"no ROS 2 relay on {self.config.socket}: {exc}") from exc
        self._observed.clear()
        with self._lock:
            self._sock = sock
        threading.Thread(target=self._read, args=(sock,), name="ros2-relay", daemon=True).start()

    def close(self) -> bool:
        """Hold where the robot is, then disconnect. Returns whether the hold was sent: a ROS
        position controller keeps its last command after the relay goes."""
        held = self.apply_response(FaultResponse.HOLD_POSITION)
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()
        return held

    def _read(self, sock: socket.socket) -> None:
        stream = sock.makefile("rb")
        try:
            while (frame := read_frame(stream)) is not None:
                header, payload = frame
                if header["kind"] == "state":
                    self._on_state(header)
                elif header["kind"] == "image":
                    self._on_image(header, payload)
        except (OSError, ValueError) as exc:
            log.warning("the ROS 2 relay connection failed: %s", exc)
        with self._lock:
            if self._sock is sock:
                self._sock = None
        self.counters["relay_lost"] += 1

    def _on_state(self, header: dict[str, object]) -> None:
        position = header["position"]
        assert isinstance(position, dict)
        state = {
            j.name: float(position[j.ros]) * j.scale + j.offset
            for j in self.config.joints
            if j.ros in position
        }
        at = int(str(header["t_us"]))
        with self._lock:
            self._state, self._state_us = state, at
            commanded = dict(self._commanded) if self._commanded else None
            self._note_observed()
        self.telemetry.append(Sample(at, state, commanded))

    def _on_image(self, header: dict[str, object], payload: bytes) -> None:
        role, encoding = str(header["camera"]), header.get("encoding")
        order = {c.role: c.color_order.value for c in self._robot.spec.cameras}.get(role)
        if order is None or encoding not in ("rgb8", "bgr8"):
            self.counters["image_unusable"] += 1
            return
        if encoding != f"{order}8":  # the same three bytes per pixel, in the other order
            payload = np.frombuffer(payload, np.uint8).reshape(-1, 3)[:, ::-1].tobytes()
        frame = ImageFrame(
            int(str(header["width"])), int(str(header["height"])), payload, int(str(header["t_us"]))
        )
        with self._lock:
            self._images[role] = frame
            self._note_observed()

    def _note_observed(self) -> None:
        if len(self._state) == len(self.config.joints) and set(self._images) >= set(
            self.config.cameras
        ):
            self._observed.set()

    def _fresh(self, now_us: int) -> bool:
        return (
            self._sock is not None
            and len(self._state) == len(self.config.joints)
            and now_us - self._state_us <= self.config.state_timeout_ms * 1000
        )

    # --- the runtime's controller and sensors

    def has_authority(self) -> bool:
        with self._lock:
            return self._fresh(time.monotonic_ns() // 1000)

    def command(self, action: NDArray[np.float64]) -> CommandResult:
        targets = dict(zip(self.commands, (float(v) for v in action), strict=True))
        if not all(math.isfinite(v) for v in targets.values()):
            return CommandResult(False, "a command value is not finite")
        result = self._send(targets)
        if result.ok:
            self.counters["commands"] += 1
            self.last_command = np.asarray(action, dtype=np.float64).copy()
        return result

    def _send(self, targets: dict[str, float]) -> CommandResult:
        joints = {j.name: j for j in self.config.joints}
        try:
            ros = [(targets[n] - joints[n].offset) / joints[n].scale for n in self.commands]
        except KeyError as missing:
            return CommandResult(False, f"no ROS joint is mapped to {missing}")
        with self._lock:
            sock = self._sock
            if sock is None or not self._fresh(time.monotonic_ns() // 1000):
                self.counters["command_refused_stale"] += 1
                return CommandResult(False, "the ROS 2 robot's joint states are stale")
            try:
                send_frame(sock, {"kind": "command", "position": ros})
            except OSError as exc:
                return CommandResult(False, f"the ROS 2 relay: {exc}")
            self._commanded = targets
        return CommandResult(True)

    def apply_response(self, response: FaultResponse) -> bool:
        if response not in (FaultResponse.HOLD_POSITION, FaultResponse.CONTROLLED_STOP):
            return False  # a ROS position controller offers no other response through the relay
        with self._lock:
            state = dict(self._state)
        if len(state) != len(self.config.joints):
            return False
        return self._send(state).ok

    def capture(self, now_us: int) -> SensorSnapshot | None:
        with self._lock:
            if not self._fresh(now_us):
                return None
            images = {c: self._images[c] for c in self.config.cameras if c in self._images}
            if len(images) != len(self.config.cameras):
                return None
            return SensorSnapshot(dict(self._state), self._state_us, images)

    def sample(self, now_us: int | None = None) -> dict[str, float] | None:
        with self._lock:
            return dict(self._state) if self._state else None

    # --- recovery

    def recover(self) -> None:
        """Before a reset: reconnect to the relay if it went. Runs in the control loop, so it
        does not wait for the robot: revalidate says whether its joint states are fresh yet."""
        if self._sock is None:
            self._open()

    def revalidate(self) -> list[str]:
        problems = []
        if self._sock is None:
            problems.append("the ROS 2 relay is not connected")
        unmapped = set(self.commands) - {j.name for j in self.config.joints}
        if unmapped:
            problems.append(f"no ROS joint is mapped to {', '.join(sorted(unmapped))}")
        if not self.has_authority():
            problems.append("no fresh joint states from the ROS 2 robot")
        return problems

    def adopt(self, robot: RobotProfile) -> None:
        self._robot = robot
