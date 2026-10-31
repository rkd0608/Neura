"""``neura-arm``: serve a real arm through a site gateway.

neura-arm check CONFIG [--robot-profile FILE]
    connect the arm without moving it, check its calibration and the robot profile against it,
    print where every joint is, and let go (the arm holds, by default)
neura-arm run CONFIG
    serve it: the runtime, a session with the gateway, and operator commands on standard input
    (arm, stop, takeover, reset, instruction TEXT, status, quit). Nothing moves until the
    operator types arm. Ctrl-C ends the task and exits.

CONFIG is a YAML file: examples/robots/so101-arm.yaml for an SO-101 on its servo bus,
examples/robots/ros2-arm.yaml for a ROS 2 robot through the relay node (``neura_robots.ros2``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from neura_contracts import RobotProfile, load_document
from neura_robots.lock import ActuatorBusyError
from neura_robots.process import ArmProcess, AssignedArm, OperatorInbox
from neura_robots.ros2.arm import Ros2Arm, Ros2Config, Ros2Joint
from neura_robots.so101 import ArmError, CameraConfig, So101Arm, So101Config
from neura_runtime.binding import ExecutionBinding, load_binding
from neura_runtime.grpc_client import RobotCredentials


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CredentialsConfig(_Model):
    # Mutual TLS (design §17.3): <robot_id>.key, <robot_id>.pem and ca.pem from the site CA.
    cert_dir: Path | None = None
    # The gateway name to verify when the gateway address is not in its certificate.
    tls_server_name: str | None = None
    # A device token, for a development gateway without TLS only.
    token: str | None = None

    @model_validator(mode="after")
    def _one(self) -> CredentialsConfig:
        if (self.cert_dir is None) == (self.token is None):
            raise ValueError("set cert_dir (mutual TLS) or token (development only)")
        return self


class RevisionConfig(_Model):
    id: str
    artifact: Path
    robot_profile: Path
    execution_profile: Path


class ArmConfig(_Model):
    type: Literal["so101"] = "so101"
    port: str
    calibration_id: str
    calibration_dir: Path | None = None
    max_relative_target: float | None = Field(default=10.0, gt=0)
    on_exit: Literal["hold", "release"] = "hold"
    hold_tolerance: float = Field(default=0.5, gt=0)
    stop_command: list[str] | None = None
    lock_dir: Path | None = None


class Ros2JointEntry(_Model):
    ros: str  # the joint's name in the robot's JointState
    name: str  # the robot profile's state and command name
    # profile = ros * scale + offset: from radians to degrees, scale is 57.29577951308232
    scale: float = Field(default=1.0, allow_inf_nan=False)
    offset: float = Field(default=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _invertible(self) -> Ros2JointEntry:
        if self.scale == 0:
            raise ValueError(f"{self.name}: scale 0 cannot map a command back to ROS")
        return self


class Ros2ArmConfig(_Model):
    type: Literal["ros2"]
    socket: Path  # the relay node's Unix socket
    joints: list[Ros2JointEntry] = Field(min_length=1)  # in the relay's --joints order
    state_timeout_ms: float = Field(default=100.0, gt=0)
    connect_timeout_s: float = Field(default=5.0, gt=0)
    # A ROS position controller keeps its last command: Neura holds where the robot is.
    on_exit: Literal["hold"] = "hold"


class CameraEntry(_Model):
    # ros2: frames from the ROS 2 relay node, which subscribes to the camera's topic
    type: Literal["opencv", "synthetic", "ros2"] = "opencv"
    index_or_path: int | str | None = None
    fps: float = Field(default=30.0, gt=0)


class RobotConfig(_Model):
    robot_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    gateway: str
    credentials: CredentialsConfig
    # Contracts to serve; without them, whatever revision the site assigns this robot.
    revision: RevisionConfig | None = None
    arm: ArmConfig | Ros2ArmConfig
    cameras: dict[str, CameraEntry] = Field(default_factory=dict)
    instruction: str | None = None
    # Where the robot spools recorded episodes when its site records them (design §28.2); unset,
    # the robot never records, whatever the site asks.
    capture_dir: Path | None = None

    @model_validator(mode="after")
    def _cameras_match_the_arm(self) -> RobotConfig:
        ros2 = isinstance(self.arm, Ros2ArmConfig)
        wrong = sorted(role for role, c in self.cameras.items() if (c.type == "ros2") != ros2)
        if wrong:
            kind = "ros2: a ROS 2 robot's frames come" if ros2 else "not ros2: only a ROS 2 robot's"
            raise ValueError(f"cameras {', '.join(wrong)} must be {kind} through the relay")
        return self

    @classmethod
    def load(cls, path: Path) -> RobotConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        base = path.parent
        config = cls.model_validate(data)

        def rel(value: Path | None) -> Path | None:
            return None if value is None else (base / value.expanduser()).resolve()

        revision = config.revision
        if revision is not None:
            revision = revision.model_copy(
                update={
                    "artifact": rel(revision.artifact),
                    "robot_profile": rel(revision.robot_profile),
                    "execution_profile": rel(revision.execution_profile),
                }
            )
        credentials = config.credentials.model_copy(
            update={"cert_dir": rel(config.credentials.cert_dir)}
        )
        arm: ArmConfig | Ros2ArmConfig
        if isinstance(config.arm, Ros2ArmConfig):
            arm = config.arm.model_copy(update={"socket": rel(config.arm.socket)})
        else:
            arm = config.arm.model_copy(
                update={
                    "calibration_dir": rel(config.arm.calibration_dir),
                    "lock_dir": rel(config.arm.lock_dir),
                }
            )
        return config.model_copy(
            update={
                "revision": revision,
                "credentials": credentials,
                "arm": arm,
                "capture_dir": rel(config.capture_dir),
            }
        )

    def so101(self) -> So101Config:
        arm = self.arm
        if not isinstance(arm, ArmConfig):
            raise SystemExit("this configuration is not an SO-101's (arm: type: so101)")
        return So101Config(
            port=arm.port,
            calibration_id=arm.calibration_id,
            calibration_dir=arm.calibration_dir,
            cameras={
                role: CameraConfig(c.type, c.index_or_path, c.fps)
                for role, c in self.cameras.items()
                if c.type != "ros2"  # none are: _cameras_match_the_arm
            },
            max_relative_target=arm.max_relative_target,
            on_exit=arm.on_exit,
            hold_tolerance=arm.hold_tolerance,
            stop_command=arm.stop_command,
            lock_dir=arm.lock_dir,
        )

    def open_arm(self, profile: RobotProfile) -> So101Arm | Ros2Arm:
        """The configured arm, not yet connected."""
        arm = self.arm
        if not isinstance(arm, Ros2ArmConfig):
            return So101Arm(self.so101(), profile)
        joints = [Ros2Joint(j.ros, j.name, j.scale, j.offset) for j in arm.joints]
        config = Ros2Config(
            arm.socket, joints, list(self.cameras), arm.state_timeout_ms, arm.connect_timeout_s
        )
        return Ros2Arm(config, profile)

    def robot_credentials(self) -> RobotCredentials:
        c = self.credentials
        if c.cert_dir is not None:
            loaded = RobotCredentials.from_directory(c.cert_dir, self.robot_id)
            return dataclasses.replace(loaded, server_name=c.tls_server_name)
        return RobotCredentials(token=c.token)

    def binding(self) -> ExecutionBinding | None:
        r = self.revision
        return None if r is None else load_binding(r.artifact, r.robot_profile, r.execution_profile)


def _say(line: str) -> None:
    print(line, flush=True)


def _where(arm: So101Arm | Ros2Arm) -> str:
    return arm.lock.device if isinstance(arm, So101Arm) else f"the relay on {arm.config.socket}"


def check(config: RobotConfig, robot_profile: Path | None) -> int:
    if robot_profile is not None:
        profile = load_document(robot_profile.read_text(encoding="utf-8"))
        if not isinstance(profile, RobotProfile):
            raise SystemExit(f"{robot_profile} is not a robot profile")
    else:
        binding = config.binding()
        if binding is None:
            raise SystemExit("give --robot-profile: this robot runs what the site assigns it")
        profile = binding.robot
    arm = config.open_arm(profile)
    try:
        arm.connect()
    except (ArmError, ActuatorBusyError) as exc:
        print(f"not ready: {exc}", file=sys.stderr)
        return 1
    try:
        positions = arm.sample() or {}
        snapshot = arm.capture(time.monotonic_ns() // 1000)
        cameras = {
            role: {"width": f.width, "height": f.height}
            for role, f in (snapshot.images.items() if snapshot else [])
        }
        print(
            json.dumps(
                {
                    "ready": True,
                    "port": _where(arm),
                    "positions": {k: round(v, 2) for k, v in positions.items()},
                    "cameras": cameras,
                    "on_exit": config.arm.on_exit,
                },
                indent=2,
            )
        )
    finally:
        arm.close()
    return 0


def _read_operator(inbox: OperatorInbox) -> None:
    for line in sys.stdin:
        inbox.submit(line)


async def serve(config: RobotConfig, *, status_every_s: float | None = None) -> int:
    inbox = OperatorInbox()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = 0

    def interrupted() -> None:
        nonlocal signals
        signals += 1
        if signals == 1:
            _say("stopping: ending the task and closing the session (again to exit now)")
            inbox.submit("quit")
            loop.call_later(3.0, stop.set)
        else:
            stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, interrupted)
    threading.Thread(target=_read_operator, args=(inbox,), daemon=True).start()

    binding = config.binding()
    credentials = config.robot_credentials()
    arm: So101Arm | Ros2Arm | None = None
    current: list[Any] = []

    async def report() -> None:
        while status_every_s and not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=status_every_s)
            process = current[0] if current else None
            if isinstance(process, AssignedArm):
                process = process.process
            if process is not None:
                print(json.dumps(process.status()), file=sys.stderr, flush=True)

    reporter = asyncio.create_task(report())
    try:
        if binding is not None:
            arm = config.open_arm(binding.robot)
            arm.connect()
            process = ArmProcess(
                arm,
                binding,
                config.robot_id,
                config.revision.id if config.revision else "",
                config.gateway,
                credentials,
                inbox=inbox,
                instruction=config.instruction,
                say=_say,
                on_quit=stop.set,
            )
            current.append(process)
            _say(f"{config.robot_id}: connected to the arm on {_where(arm)}; serving")
            await process.connection.run(stop)
        else:

            def open_arm(profile: RobotProfile) -> So101Arm | Ros2Arm:
                nonlocal arm
                arm = config.open_arm(profile)
                arm.connect()
                _say(f"{config.robot_id}: connected to the arm on {_where(arm)}")
                return arm

            assigned = AssignedArm(
                open_arm,
                config.robot_id,
                gateway=config.gateway,
                credentials=credentials,
                inbox=inbox,
                instruction=config.instruction,
                say=_say,
                capture_dir=config.capture_dir,
            )
            current.append(assigned)
            _say(f"{config.robot_id}: waiting for the site to assign a revision")
            await assigned.run(stop)
    except (ArmError, ActuatorBusyError) as exc:
        print(f"cannot serve: {exc}", file=sys.stderr)
        return 1
    finally:
        stop.set()
        await reporter
        if arm is not None:
            on_exit = config.arm.on_exit
            if arm.close():
                _say(f"{config.robot_id}: let go of the arm ({on_exit})")
            else:
                _say(f"{config.robot_id}: let go of the arm; the {on_exit} was NOT confirmed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neura-arm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check_parser = sub.add_parser("check", help="connect without moving and check the contract")
    check_parser.add_argument("config", type=Path)
    check_parser.add_argument("--robot-profile", type=Path)
    run_parser = sub.add_parser("run", help="serve the arm")
    run_parser.add_argument("config", type=Path)
    run_parser.add_argument("--status-every", type=float, default=None, metavar="SECONDS")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("NEURA_LOG_LEVEL", "warning").upper())
    config = RobotConfig.load(args.config)
    if args.command == "check":
        return check(config, args.robot_profile)
    return asyncio.run(serve(config, status_every_s=args.status_every))


if __name__ == "__main__":
    sys.exit(main())
