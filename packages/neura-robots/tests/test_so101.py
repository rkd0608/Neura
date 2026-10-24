"""The SO-101 integration on the emulated arm, through LeRobot's real Feetech bus driver."""

from __future__ import annotations

import array
import errno
import fcntl
import os
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

pytest.importorskip("lerobot", reason="needs the models group: uv sync --group models")

from neura_contracts import FaultResponse, RobotProfile, load_document
from neura_robots.emulator import SO101Emulator, write_calibration
from neura_robots.lock import ActuatorBusyError
from neura_robots.so101 import ArmError, CameraConfig, So101Arm, So101Config

ROOT = Path(__file__).resolve().parents[3]
CAMERAS = {"front": CameraConfig("synthetic"), "wrist": CameraConfig("synthetic")}


def _profile() -> RobotProfile:
    document = load_document((ROOT / "examples/contracts/so101.robot.yaml").read_text())
    assert isinstance(document, RobotProfile)
    return document


def _with_range(profile: RobotProfile, name: str, low: float, high: float) -> RobotProfile:
    commands = [
        c.model_copy(update={"min": low, "max": high}) if c.name == name else c
        for c in profile.spec.command_interface
    ]
    return profile.model_copy(
        update={"spec": profile.spec.model_copy(update={"command_interface": commands})}
    )


@pytest.fixture
def emulator() -> Iterator[SO101Emulator]:
    with SO101Emulator(positions_deg={"shoulder_pan": 20.0, "elbow_flex": -15.0}) as e:
        yield e


@pytest.fixture
def config(emulator: SO101Emulator, tmp_path: Path) -> So101Config:
    write_calibration(emulator, tmp_path / "calibration", "arm-1")
    return So101Config(
        port=emulator.port,
        calibration_id="arm-1",
        calibration_dir=tmp_path / "calibration",
        cameras=CAMERAS,
        lock_dir=tmp_path / "locks",
    )


@pytest.fixture
def arm(config: So101Config) -> Iterator[So101Arm]:
    arm = So101Arm(config, _profile())
    arm.connect()
    yield arm
    arm.close()


def _vector(arm: So101Arm, **degrees: float) -> NDArray[np.float64]:
    current = arm.sample()
    assert current is not None
    values = {**current, **{f"{k}.pos": v for k, v in degrees.items()}}
    return np.array([values[name] for name in arm.commands], dtype=np.float64)


def _settle(seconds: float = 0.3) -> None:
    time.sleep(seconds)


def test_connect_never_executes_a_goal_left_in_the_servos(
    emulator: SO101Emulator, config: So101Config
) -> None:
    servo = emulator.servo("shoulder_pan")
    with emulator._lock:  # an earlier program left a goal far from where the arm is
        servo._put(42, 2, (servo.present() + 900) % 4096)
    arm = So101Arm(config, _profile())
    arm.connect()
    try:
        assert all(emulator.torque_enabled().values())
        _settle()
        assert emulator.positions()["shoulder_pan"] == pytest.approx(20.0, abs=0.5)
    finally:
        arm.close()


def test_commands_move_the_arm_within_the_per_command_limit(
    arm: So101Arm, emulator: SO101Emulator
) -> None:
    assert arm.command(_vector(arm, shoulder_pan=25.0)).ok
    _settle()
    assert emulator.positions()["shoulder_pan"] == pytest.approx(25.0, abs=0.5)
    assert arm.counters["command_clipped"] == 0

    assert arm.command(_vector(arm, shoulder_pan=60.0)).ok  # 35° away: limited to 10°
    _settle()
    assert emulator.positions()["shoulder_pan"] == pytest.approx(35.0, abs=0.5)
    assert arm.counters["command_clipped"] == 1


def test_hold_is_confirmed_and_holds(arm: So101Arm, emulator: SO101Emulator) -> None:
    assert arm.command(_vector(arm, elbow_flex=-5.0)).ok
    time.sleep(0.01)  # mid-move
    assert arm.apply_response(FaultResponse.HOLD_POSITION)
    stopped = emulator.positions()["elbow_flex"]
    _settle()
    assert emulator.positions()["elbow_flex"] == pytest.approx(stopped, abs=0.5)
    assert arm.apply_response(FaultResponse.CONTROLLED_STOP)
    assert not arm.apply_response(FaultResponse.BRAKE)  # dropping torque is not a stop here


def test_hold_is_not_confirmed_when_a_joint_has_no_torque(
    arm: So101Arm, emulator: SO101Emulator
) -> None:
    emulator.set_fault("wrist_flex", overload=True)
    assert not arm.apply_response(FaultResponse.HOLD_POSITION)


def test_a_joint_that_loses_torque_fails_the_command(
    config: So101Config, emulator: SO101Emulator
) -> None:
    arm = So101Arm(replace(config, torque_check_every=1), _profile())
    arm.connect()
    try:
        assert arm.command(_vector(arm)).ok
        emulator.set_fault("shoulder_lift", overload=True)
        result = arm.command(_vector(arm))
        assert not result.ok
        assert "shoulder_lift" in (result.detail or "")
    finally:
        arm.close()


def test_capture_is_stamped_on_the_robot_clock(arm: So101Arm) -> None:
    now = time.monotonic_ns() // 1000
    snapshot = arm.capture(now)
    assert snapshot is not None
    assert set(snapshot.state) == set(arm.states)
    assert snapshot.state["shoulder_pan.pos"] == pytest.approx(20.0, abs=0.5)
    assert snapshot.state_time_us == now
    assert set(snapshot.images) == {"front", "wrist"}
    for frame in snapshot.images.values():
        assert (frame.width, frame.height) == (640, 480)
        assert len(frame.data) == 640 * 480 * 3
        assert now - 200_000 < frame.capture_time_us <= now


def test_a_frozen_camera_shows_its_age(arm: So101Arm) -> None:
    arm.cameras["wrist"].frozen = True
    time.sleep(0.3)
    now = time.monotonic_ns() // 1000
    snapshot = arm.capture(now)
    assert snapshot is not None
    assert now - snapshot.images["wrist"].capture_time_us > 200_000  # the runtime rejects it
    assert now - snapshot.images["front"].capture_time_us < 100_000


def test_one_runtime_per_arm(arm: So101Arm, config: So101Config) -> None:
    with pytest.raises(ActuatorBusyError, match="another process"):
        So101Arm(config, _profile()).connect()
    assert arm.has_authority()


def test_authority_is_lost_with_the_lock_file_or_the_bus(arm: So101Arm) -> None:
    assert arm.has_authority()
    os.unlink(arm.lock.path)
    arm.lock.path.write_text("someone else\n")
    assert not arm.has_authority()


def test_a_silent_bus_fails_reads_and_commands(arm: So101Arm, emulator: SO101Emulator) -> None:
    emulator.set_fault(silent=True)
    assert arm.capture(time.monotonic_ns() // 1000) is None
    assert not arm.command(np.zeros(len(arm.commands))).ok  # its present position is unknown
    assert not arm.apply_response(FaultResponse.HOLD_POSITION)


def test_a_profile_wider_than_the_calibration_is_refused(config: So101Config) -> None:
    wide = _with_range(_profile(), "shoulder_pan.pos", -150.0, 150.0)
    arm = So101Arm(config, wide)
    with pytest.raises(ArmError, match=r"shoulder_pan\.pos: profile allows \[-150, 150\]"):
        arm.connect()
    So101Arm(config, _profile()).close()  # the actuator lock was released
    retry = So101Arm(config, _profile())
    retry.connect()
    retry.close()


def test_an_arm_that_does_not_match_its_calibration_is_refused(
    config: So101Config, emulator: SO101Emulator, tmp_path: Path
) -> None:
    other = SO101Emulator()
    other.calibration["elbow_flex"].homing_offset += 40
    write_calibration(other, tmp_path / "calibration", "arm-1")
    other.stop()
    with pytest.raises(ArmError, match="lerobot-calibrate"):
        So101Arm(config, _profile()).connect()


def test_a_camera_missing_from_the_configuration_is_refused(config: So101Config) -> None:
    with pytest.raises(ArmError, match="wrist"):
        So101Arm(replace(config, cameras={"front": CAMERAS["front"]}), _profile()).connect()


TIOCGEXCL = 0x80045440  # Linux: _IOR('T', 0x40, int)


def _exclusive(port: str) -> bool:
    """Whether the serial port refuses new opens (TIOCEXCL). Root opens it regardless and can
    ask; anyone else is refused with EBUSY."""
    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as error:
        if error.errno == errno.EBUSY:
            return True
        raise
    try:
        flag = array.array("i", [0])
        fcntl.ioctl(fd, TIOCGEXCL, flag)
        return bool(flag[0])
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "linux", reason="TIOCGEXCL is Linux's")
def test_closing_lets_the_port_be_opened_again(
    config: So101Config, emulator: SO101Emulator
) -> None:
    arm = So101Arm(config, _profile())
    arm.connect()
    assert _exclusive(emulator.port)  # nobody else drives it while Neura does
    arm.close()
    # The emulator keeps the terminal open, as a USB adapter's driver may: the flag must be
    # cleared before the port closes, or nothing could open it again.
    assert not _exclusive(emulator.port)


def test_close_holds_or_releases(config: So101Config, emulator: SO101Emulator) -> None:
    arm = So101Arm(config, _profile())
    arm.connect()
    arm.close()
    assert all(emulator.torque_enabled().values())  # on_exit=hold: the arm stays up
    arm = So101Arm(replace(config, on_exit="release"), _profile())
    arm.connect()
    arm.close()
    assert not any(emulator.torque_enabled().values())


def test_telemetry_records_measured_and_commanded(arm: So101Arm) -> None:
    arm.command(_vector(arm, shoulder_pan=22.0))
    arm.sample()
    last = arm.telemetry[-1]
    assert last.commanded is not None
    assert last.commanded["shoulder_pan.pos"] == pytest.approx(22.0)
    assert set(last.measured) == set(arm.commands)
