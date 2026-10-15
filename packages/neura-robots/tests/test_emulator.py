"""The servo bus emulator at the byte level (Feetech protocol 0), without LeRobot."""

from __future__ import annotations

import os
import select
import termios
import time
import tty
from collections.abc import Iterator

import pytest

from neura_robots import emulator as em


def _packet(motor_id: int, instruction: int, params: bytes = b"") -> bytes:
    body = bytes([motor_id, len(params) + 2, instruction]) + params
    return b"\xff\xff" + body + bytes([~sum(body) & 0xFF])


class Bus:
    def __init__(self, port: str) -> None:
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        attributes = termios.tcgetattr(self.fd)
        attributes[3] &= ~termios.ECHO
        termios.tcsetattr(self.fd, termios.TCSANOW, attributes)

    def send(self, packet: bytes) -> None:
        os.write(self.fd, packet)

    def receive(self, count: int, timeout: float = 0.5) -> list[tuple[int, int, bytes]]:
        """Read ``count`` status packets: (id, error, params)."""
        buffer = b""
        out: list[tuple[int, int, bytes]] = []
        deadline = time.monotonic() + timeout
        while len(out) < count and time.monotonic() < deadline:
            readable, _, _ = select.select([self.fd], [], [], 0.05)
            if readable:
                buffer += os.read(self.fd, 4096)
            while len(buffer) >= 4 and len(buffer) >= buffer[3] + 4:
                assert buffer[:2] == b"\xff\xff"
                total = buffer[3] + 4
                packet, buffer = buffer[:total], buffer[total:]
                assert packet[-1] == ~sum(packet[2:-1]) & 0xFF, "bad checksum"
                out.append((packet[2], packet[4], packet[5:-1]))
        return out

    def ask(self, packet: bytes, count: int = 1) -> list[tuple[int, int, bytes]]:
        self.send(packet)
        return self.receive(count)

    def read(self, motor_id: int, address: int, length: int) -> int:
        [(_, _, data)] = self.ask(_packet(motor_id, em.INST_READ, bytes([address, length])))
        return int.from_bytes(data, "little")

    def write(self, motor_id: int, address: int, value: int, length: int) -> None:
        params = bytes([address]) + value.to_bytes(length, "little")
        assert len(self.ask(_packet(motor_id, em.INST_WRITE, params))) == 1

    def close(self) -> None:
        os.close(self.fd)


@pytest.fixture
def arm() -> Iterator[tuple[em.SO101Emulator, Bus]]:
    with em.SO101Emulator(positions_deg={"elbow_flex": 30.0}) as emulator:
        bus = Bus(emulator.port)
        yield emulator, bus
        bus.close()


def test_ping_reports_every_servo_in_id_order(arm: tuple[em.SO101Emulator, Bus]) -> None:
    emulator, bus = arm
    replies = bus.ask(_packet(em.BROADCAST_ID, em.INST_PING), count=6)
    assert [r[0] for r in replies] == [1, 2, 3, 4, 5, 6]
    assert bus.read(3, em.ADDR_MODEL, 2) == em.MODEL_NUMBER
    emulator.set_fault("wrist_flex", unplugged=True)
    replies = bus.ask(_packet(em.BROADCAST_ID, em.INST_PING), count=6)
    assert [r[0] for r in replies] == [1, 2, 3, 5, 6]


def test_present_position_follows_the_goal_only_with_torque(
    arm: tuple[em.SO101Emulator, Bus],
) -> None:
    emulator, bus = arm
    start = bus.read(3, em.ADDR_PRESENT_POSITION, 2)
    bus.write(3, em.ADDR_GOAL_POSITION, start + 300, 2)
    time.sleep(0.15)
    assert abs(bus.read(3, em.ADDR_PRESENT_POSITION, 2) - start) <= 1  # torque is off
    bus.write(3, em.ADDR_TORQUE_ENABLE, 1, 1)
    time.sleep(0.3)  # 300 steps at 3,000 steps/s
    assert abs(bus.read(3, em.ADDR_PRESENT_POSITION, 2) - (start + 300)) <= 2
    assert emulator.positions()["elbow_flex"] == pytest.approx(30.0 + 300 * 360 / 4095, abs=0.5)


def test_sync_read_and_sync_write(arm: tuple[em.SO101Emulator, Bus]) -> None:
    _, bus = arm
    ids = bytes([1, 2, 3, 4, 5, 6])
    bus.send(
        _packet(
            em.BROADCAST_ID,
            em.INST_SYNC_WRITE,
            bytes([em.ADDR_GOAL_POSITION, 2])
            + b"".join(bytes([i]) + (1000 + i).to_bytes(2, "little") for i in ids),
        )
    )
    replies = bus.ask(
        _packet(em.BROADCAST_ID, em.INST_SYNC_READ, bytes([em.ADDR_GOAL_POSITION, 2]) + ids),
        count=6,
    )
    assert [(r[0], int.from_bytes(r[2], "little")) for r in replies] == [(i, 1000 + i) for i in ids]


def test_eeprom_is_read_only_while_locked(arm: tuple[em.SO101Emulator, Bus]) -> None:
    _, bus = arm
    bus.write(1, em.ADDR_LOCK, 1, 1)
    bus.write(1, em.ADDR_MIN_POSITION, 10, 2)
    assert bus.read(1, em.ADDR_MIN_POSITION, 2) != 10
    bus.write(1, em.ADDR_LOCK, 0, 1)
    bus.write(1, em.ADDR_MIN_POSITION, 10, 2)
    assert bus.read(1, em.ADDR_MIN_POSITION, 2) == 10


def test_faults(arm: tuple[em.SO101Emulator, Bus]) -> None:
    emulator, bus = arm
    bus.write(2, em.ADDR_TORQUE_ENABLE, 1, 1)
    emulator.set_fault("shoulder_lift", overload=True)
    [(_, error, _)] = bus.ask(_packet(2, em.INST_PING))
    assert error & em.ERROR_OVERLOAD
    assert bus.read(2, em.ADDR_TORQUE_ENABLE, 1) == 0

    emulator.set_fault(silent=True)
    assert bus.ask(_packet(1, em.INST_PING)) == []
    emulator.set_fault(silent=False, corrupt_probability=1.0)
    with pytest.raises(AssertionError, match="bad checksum"):
        bus.ask(_packet(1, em.INST_PING))


def test_corrupted_instructions_are_ignored(arm: tuple[em.SO101Emulator, Bus]) -> None:
    _, bus = arm
    packet = bytearray(_packet(1, em.INST_PING))
    packet[-1] ^= 0xFF
    assert bus.ask(bytes(packet)) == []
    assert len(bus.ask(_packet(1, em.INST_PING))) == 1  # and the bus resynchronizes


def test_gravity_sags_a_joint_without_torque() -> None:
    with em.SO101Emulator(gravity=500.0) as emulator:
        before = emulator.positions()["shoulder_lift"]
        time.sleep(0.2)
        assert emulator.positions()["shoulder_lift"] < before - 5


def test_calibration_file_matches_lerobot_layout(tmp_path: os.PathLike[str]) -> None:
    import json
    from pathlib import Path

    with em.SO101Emulator() as emulator:
        path = em.write_calibration(emulator, Path(tmp_path), "arm-1")
    data = json.loads(path.read_text())
    assert path.name == "arm-1.json"
    assert set(data) == {name for name, _ in em.SO101_MOTORS}
    assert set(data["gripper"]) == {"id", "drive_mode", "homing_offset", "range_min", "range_max"}
