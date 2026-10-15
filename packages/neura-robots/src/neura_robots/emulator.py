"""An emulated SO-101 servo bus: six Feetech STS3215 servos behind a pseudo-terminal.

``neura-arm-emulator`` prints a serial port path; LeRobot's real ``FeetechMotorsBus`` (and the
Feetech SDK under it) opens it like the USB serial adapter of a real arm. Nothing above the serial
port knows the difference, so the SO-101 integration, ``neura-arm`` and ``neura-qualify`` run
end to end without hardware, in development and in CI.

What is emulated (Feetech protocol 0, the instructions LeRobot uses):

- PING (also broadcast), READ, WRITE, SYNC_READ, SYNC_WRITE, REG_WRITE and ACTION, with
  checksums; status packets in ID order.
- The STS3215 control table: model number 777, firmware version, ID, limits, homing offset
  (sign-magnitude, Present = Actual - Homing_Offset), operating mode, torque enable, EEPROM lock
  (EEPROM writes are ignored while locked), goal and present position, moving flag.
- Dynamics: with torque enabled, each servo moves toward its goal at up to ``max_speed`` steps per
  second (about 3,000 for an STS3215 at 7.4 V); with torque disabled it stays where it is, or
  sags under ``gravity`` steps per second if set.
- Faults, for tests and qualification: an unplugged servo (no answer), corrupted status
  packets, a stalled (blocked) joint, an overload that drops torque, and a silent bus.

It is a test double for the bus, not a simulation of the arm's mechanics or cameras.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import select
import signal
import sys
import termios
import threading
import time
import tty
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

MODEL_NUMBER = 777  # STS3215
RESOLUTION = 4096
FIRMWARE = (3, 10)
BROADCAST_ID = 0xFE

# Control table addresses (STS/SMS series).
ADDR_FIRMWARE_MAJOR = 0
ADDR_FIRMWARE_MINOR = 1
ADDR_MODEL = 3
ADDR_ID = 5
ADDR_BAUD = 6
ADDR_MIN_POSITION = 9
ADDR_MAX_POSITION = 11
ADDR_MAX_TORQUE = 16
ADDR_PHASE = 18
ADDR_HOMING_OFFSET = 31
ADDR_OPERATING_MODE = 33
ADDR_TORQUE_ENABLE = 40
ADDR_GOAL_POSITION = 42
ADDR_LOCK = 55
ADDR_PRESENT_POSITION = 56
ADDR_PRESENT_VELOCITY = 58
ADDR_PRESENT_LOAD = 60
ADDR_PRESENT_VOLTAGE = 62
ADDR_PRESENT_TEMPERATURE = 63
ADDR_STATUS = 65
ADDR_MOVING = 66
EEPROM_END = 40  # addresses below are EEPROM: ignored while Lock is set

INST_PING, INST_READ, INST_WRITE, INST_REG_WRITE, INST_ACTION = 1, 2, 3, 4, 5
INST_SYNC_READ, INST_SYNC_WRITE = 0x82, 0x83

ERROR_OVERLOAD = 0x20

# SO-101 joints: (name, id). Positions are raw steps; the defaults leave every joint's
# calibrated range comfortably wider than the example robot profile's command ranges.
SO101_MOTORS = (
    ("shoulder_pan", 1),
    ("shoulder_lift", 2),
    ("elbow_flex", 3),
    ("wrist_flex", 4),
    ("wrist_roll", 5),
    ("gripper", 6),
)


def _decode_sign(value: int, bit: int) -> int:
    return -(value & ~(1 << bit)) if value & (1 << bit) else value


def _encode_sign(value: int, bit: int) -> int:
    return (-value | (1 << bit)) if value < 0 else value


@dataclass(slots=True)
class Calibration:
    """What `lerobot-calibrate` would write: per motor, homing offset and range (raw steps)."""

    homing_offset: int
    range_min: int
    range_max: int


def default_calibration() -> dict[str, Calibration]:
    """A plausible calibration: every joint centered at 2047 after homing, ranges of about
    ±123° (±180° for the wrist roll), and a gripper range of about 100°."""
    calibration = {}
    for name, motor_id in SO101_MOTORS:
        homing = (motor_id * 37) % 61 - 30  # small, different per joint
        if name == "wrist_roll":
            calibration[name] = Calibration(homing, 0, 4095)
        elif name == "gripper":
            calibration[name] = Calibration(homing, 1900, 3050)
        else:
            calibration[name] = Calibration(homing, 2047 - 1400, 2047 + 1400)
    return calibration


@dataclass(slots=True)
class Servo:
    motor_id: int
    memory: bytearray = field(default_factory=lambda: bytearray(256))
    actual: float = 2048.0  # raw steps, before the homing offset
    max_speed: float = 3000.0  # steps per second
    gravity: float = 0.0  # steps per second of sag with torque off
    # Faults
    unplugged: bool = False
    stalled: bool = False
    pending: dict[int, bytes] | None = None  # REG_WRITE, applied on ACTION

    def __post_init__(self) -> None:
        mem = self.memory
        mem[ADDR_FIRMWARE_MAJOR], mem[ADDR_FIRMWARE_MINOR] = FIRMWARE
        self._put(ADDR_MODEL, 2, MODEL_NUMBER)
        mem[ADDR_ID] = self.motor_id
        mem[ADDR_BAUD] = 0  # 1 Mbps
        self._put(ADDR_MIN_POSITION, 2, 0)
        self._put(ADDR_MAX_POSITION, 2, 4095)
        self._put(ADDR_MAX_TORQUE, 2, 1000)
        mem[ADDR_PHASE] = 0x10  # angle feedback mode set, as shipped; LeRobot clears it
        mem[ADDR_PRESENT_VOLTAGE] = 74
        mem[ADDR_PRESENT_TEMPERATURE] = 30
        self._put(ADDR_GOAL_POSITION, 2, self.present())

    # --- registers ------------------------------------------------------------------------------

    def _put(self, address: int, length: int, value: int) -> None:
        self.memory[address : address + length] = int(value).to_bytes(length, "little")

    def _get(self, address: int, length: int) -> int:
        return int.from_bytes(self.memory[address : address + length], "little")

    @property
    def homing_offset(self) -> int:
        return _decode_sign(self._get(ADDR_HOMING_OFFSET, 2), 11)

    @property
    def torque_enabled(self) -> bool:
        return self.memory[ADDR_TORQUE_ENABLE] == 1

    def present(self) -> int:
        return round(self.actual - self.homing_offset) % RESOLUTION

    def goal_actual(self) -> float:
        goal = _decode_sign(self._get(ADDR_GOAL_POSITION, 2), 15)
        return float(goal + self.homing_offset)

    def read(self, address: int, length: int) -> bytes:
        self._put(ADDR_PRESENT_POSITION, 2, _encode_sign(self.present(), 15))
        moving = self.torque_enabled and abs(self.goal_actual() - self.actual) > 1.0
        self.memory[ADDR_MOVING] = 1 if moving else 0
        return bytes(self.memory[address : address + length])

    def write(self, address: int, data: bytes) -> None:
        for offset, byte in enumerate(data):
            target = address + offset
            if target < EEPROM_END and self.memory[ADDR_LOCK] == 1:
                continue  # EEPROM is read-only while locked, as on the servo
            if target in (ADDR_FIRMWARE_MAJOR, ADDR_FIRMWARE_MINOR, ADDR_MODEL, ADDR_MODEL + 1):
                continue
            if ADDR_PRESENT_POSITION <= target < ADDR_PRESENT_POSITION + 16:
                continue  # read-only feedback
            self.memory[target] = byte
        if address <= ADDR_TORQUE_ENABLE < address + len(data) and self.torque_enabled:
            self.memory[ADDR_STATUS] &= ~ERROR_OVERLOAD  # re-enabling torque clears the trip

    # --- dynamics -------------------------------------------------------------------------------

    def step(self, dt: float) -> None:
        if self.torque_enabled:
            if self.stalled:
                return
            error = self.goal_actual() - self.actual
            limit = self.max_speed * dt
            self.actual += max(-limit, min(limit, error))
        elif self.gravity:
            self.actual -= self.gravity * dt
        low = self._get(ADDR_MIN_POSITION, 2) + self.homing_offset
        high = self._get(ADDR_MAX_POSITION, 2) + self.homing_offset
        if self.torque_enabled and high > low:
            self.actual = min(max(self.actual, float(low)), float(high))

    def overload(self) -> None:
        """Overload protection trips: torque drops and the error bit is reported."""
        self.memory[ADDR_TORQUE_ENABLE] = 0
        self.memory[ADDR_STATUS] |= ERROR_OVERLOAD


def _checksum(body: Iterable[int]) -> int:
    return ~sum(body) & 0xFF


def _status(motor_id: int, error: int, params: bytes = b"") -> bytes:
    body = bytes([motor_id, len(params) + 2, error]) + params
    return b"\xff\xff" + body + bytes([_checksum(body)])


class SO101Emulator:
    """Six emulated STS3215 servos on a pseudo-terminal. Use as a context manager, or call
    ``start()`` and ``stop()``. ``port`` is the path a bus opens."""

    def __init__(
        self,
        *,
        calibration: dict[str, Calibration] | None = None,
        positions_deg: dict[str, float] | None = None,
        max_speed: float = 3000.0,
        gravity: float = 0.0,
        seed: int = 0,
    ) -> None:
        self.calibration = calibration or default_calibration()
        self.servos: dict[int, Servo] = {}
        self.names: dict[str, int] = {}
        for name, motor_id in SO101_MOTORS:
            cal = self.calibration[name]
            servo = Servo(motor_id, max_speed=max_speed, gravity=gravity)
            servo._put(ADDR_HOMING_OFFSET, 2, _encode_sign(cal.homing_offset, 11))
            servo._put(ADDR_MIN_POSITION, 2, cal.range_min)
            servo._put(ADDR_MAX_POSITION, 2, cal.range_max)
            mid = (cal.range_min + cal.range_max) / 2
            degrees = (positions_deg or {}).get(name, 0.0)
            servo.actual = mid + degrees * (RESOLUTION - 1) / 360 + cal.homing_offset
            servo._put(ADDR_GOAL_POSITION, 2, servo.present())
            self.servos[motor_id] = servo
            self.names[name] = motor_id
        self.silent = False  # the whole bus stops answering (adapter unplugged, power cut)
        self.corrupt_probability = 0.0
        self.packets = 0
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._master, self._slave = os.openpty()
        tty.setraw(self._master)
        attributes = termios.tcgetattr(self._master)
        attributes[3] &= ~termios.ECHO
        termios.tcsetattr(self._master, termios.TCSANOW, attributes)
        self.port = os.ttyname(self._slave)

    # --- lifecycle ------------------------------------------------------------------------------

    def start(self) -> SO101Emulator:
        self._thread = threading.Thread(target=self._serve, name="so101-emulator", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        for fd in (self._master, self._slave):
            with contextlib.suppress(OSError):
                os.close(fd)

    def __enter__(self) -> SO101Emulator:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    # --- observation and faults (thread-safe) ---------------------------------------------------

    def servo(self, name: str) -> Servo:
        return self.servos[self.names[name]]

    def positions(self) -> dict[str, float]:
        """Every joint's true (emulated) position in LeRobot's units: degrees, and 0-100 for the
        gripper across its calibrated range."""
        with self._lock:
            out = {}
            for name, motor_id in self.names.items():
                cal = self.calibration[name]
                present = self.servos[motor_id].present()
                if name == "gripper":
                    span = max(cal.range_max - cal.range_min, 1)
                    bounded = min(max(present, cal.range_min), cal.range_max)
                    out[name] = (bounded - cal.range_min) * 100 / span
                else:
                    mid = (cal.range_min + cal.range_max) / 2
                    out[name] = (present - mid) * 360 / (RESOLUTION - 1)
            return out

    def torque_enabled(self) -> dict[str, bool]:
        with self._lock:
            return {name: self.servos[i].torque_enabled for name, i in self.names.items()}

    def set_fault(self, name: str | None = None, **flags: object) -> None:
        """``set_fault("elbow_flex", stalled=True)``, ``set_fault(silent=True)``, or
        ``set_fault(corrupt_probability=0.2)``."""
        with self._lock:
            if name is None:
                for key, value in flags.items():
                    setattr(self, key, value)
                return
            servo = self.servo(name)
            for key, value in flags.items():
                if key == "overload" and value:
                    servo.overload()
                else:
                    setattr(servo, key, value)

    # --- the bus ----------------------------------------------------------------------------------

    def _serve(self) -> None:
        buffer = bytearray()
        last = time.monotonic()
        while not self._stop.is_set():
            readable, _, _ = select.select([self._master], [], [], 0.002)
            now = time.monotonic()
            with self._lock:
                for servo in self.servos.values():
                    servo.step(now - last)
            last = now
            if not readable:
                continue
            try:
                chunk = os.read(self._master, 4096)
            except OSError:
                return
            buffer.extend(chunk)
            while True:
                packet = self._take_packet(buffer)
                if packet is None:
                    break
                reply = self._handle(packet)
                if reply and not self.silent:
                    if self.corrupt_probability and self._rng.random() < self.corrupt_probability:
                        reply = reply[:-1] + bytes([reply[-1] ^ 0x5A])
                    with contextlib.suppress(OSError):
                        os.write(self._master, reply)

    @staticmethod
    def _take_packet(buffer: bytearray) -> bytes | None:
        while len(buffer) >= 2 and not (buffer[0] == 0xFF and buffer[1] == 0xFF):
            del buffer[0]
        if len(buffer) < 4:
            return None
        total = buffer[3] + 4
        if len(buffer) < total:
            return None
        packet = bytes(buffer[:total])
        del buffer[:total]
        if _checksum(packet[2:-1]) != packet[-1]:
            return b""  # corrupted instruction: ignored, as a servo would
        return packet

    def _handle(self, packet: bytes) -> bytes:
        if not packet:
            return b""
        self.packets += 1
        motor_id, instruction, params = packet[2], packet[4], packet[5:-1]
        with self._lock:
            if instruction == INST_SYNC_WRITE:
                address, length = params[0], params[1]
                data = params[2:]
                for i in range(0, len(data), length + 1):
                    servo = self.servos.get(data[i])
                    if servo is not None and not servo.unplugged:
                        servo.write(address, data[i + 1 : i + 1 + length])
                return b""
            if instruction == INST_SYNC_READ:
                address, length = params[0], params[1]
                out = b""
                for target in params[2:]:
                    servo = self.servos.get(target)
                    if servo is not None and not servo.unplugged:
                        out += _status(target, self._error(servo), servo.read(address, length))
                return out
            if motor_id == BROADCAST_ID:
                if instruction == INST_PING:
                    return b"".join(
                        _status(i, self._error(s))
                        for i, s in sorted(self.servos.items())
                        if not s.unplugged
                    )
                if instruction == INST_ACTION:
                    for servo in self.servos.values():
                        self._action(servo)
                return b""
            servo = self.servos.get(motor_id)
            if servo is None or servo.unplugged:
                return b""
            if instruction == INST_PING:
                return _status(motor_id, self._error(servo))
            if instruction == INST_READ:
                return _status(motor_id, self._error(servo), servo.read(params[0], params[1]))
            if instruction == INST_WRITE:
                servo.write(params[0], params[1:])
                return _status(motor_id, self._error(servo))
            if instruction == INST_REG_WRITE:
                servo.pending = {params[0]: bytes(params[1:])}
                return _status(motor_id, self._error(servo))
            if instruction == INST_ACTION:
                self._action(servo)
                return _status(motor_id, self._error(servo))
            return _status(motor_id, 0x40)  # instruction error

    @staticmethod
    def _action(servo: Servo) -> None:
        for address, data in (servo.pending or {}).items():
            servo.write(address, data)
        servo.pending = None

    @staticmethod
    def _error(servo: Servo) -> int:
        return servo.memory[ADDR_STATUS] & 0x7F


def lerobot_calibration(calibration: dict[str, Calibration]) -> dict[str, dict[str, int]]:
    """The calibration file LeRobot keeps for this arm (``<calibration_dir>/<robot id>.json``)."""
    return {
        name: {
            "id": motor_id,
            "drive_mode": 0,
            "homing_offset": calibration[name].homing_offset,
            "range_min": calibration[name].range_min,
            "range_max": calibration[name].range_max,
        }
        for name, motor_id in SO101_MOTORS
    }


def write_calibration(emulator: SO101Emulator, directory: Path, robot_id: str) -> Path:
    """Write the emulated arm's LeRobot calibration file, as ``lerobot-calibrate`` would."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{robot_id}.json"
    path.write_text(json.dumps(lerobot_calibration(emulator.calibration), indent=4))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neura-arm-emulator", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--calibration-out",
        type=Path,
        help="write the matching LeRobot calibration file here (e.g. <dir>/<robot id>.json)",
    )
    parser.add_argument("--gravity", type=float, default=0.0, help="sag with torque off, steps/s")
    args = parser.parse_args(argv)
    emulator = SO101Emulator(gravity=args.gravity)
    if args.calibration_out is not None:
        out: Path = args.calibration_out
        write_calibration(emulator, out.parent, out.name.removesuffix(".json"))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with emulator:
        print(emulator.port, flush=True)
        while not stop.wait(5):
            positions = emulator.positions()
            print(
                " ".join(f"{k}={v:+.1f}" for k, v in positions.items()), file=sys.stderr, flush=True
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
