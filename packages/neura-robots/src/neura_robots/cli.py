"""``neura-arm``: serve a real arm through a site gateway.

neura-arm check CONFIG [--robot-profile FILE]
    connect the arm without moving it, check its calibration and the robot profile against it,
    print where every joint is, and let go (the arm holds, by default)
neura-arm run CONFIG [--policy URI]
    serve it: the runtime, the policy (from the site gateway, or the configuration's `policy:`
    without one), and operator commands on standard input (arm, stop, takeover, reset,
    instruction TEXT, status, quit). Nothing moves until the operator types arm. Ctrl-C ends the
    task and exits. --policy picker.py or --policy neura://host:port runs that policy with the
    configuration's contracts instead of the gateway's.

CONFIG is a YAML file: examples/robots/so101-arm.yaml for an SO-101 on its servo bus,
examples/robots/ros2-arm.yaml for a ROS 2 robot through the relay node (``neura_robots.ros2``),
examples/robots/panda-sim.yaml for a simulated arm (MuJoCo Menagerie's Panda, UR5e, Gen3, xArm7
or SO-ARM100), whose contracts are inferred from its model and the policy.
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
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from neura_contracts import RobotProfile, load_document
from neura_robots.lock import ActuatorBusyError
from neura_robots.policies import PolicyConfig, describe, inferred_binding, open_policy
from neura_robots.process import ArmProcess, AssignedArm, OperatorInbox
from neura_robots.recorded import RecordedArm, RecordedEpisode
from neura_robots.recorded import describe as describe_recording
from neura_robots.ros2.arm import Ros2Arm, Ros2Config, Ros2Joint
from neura_robots.so101 import ArmError, CameraConfig, So101Arm, So101Config

if TYPE_CHECKING:
    from neura_robots.sim.arm import SimArm
    from neura_robots.sim.menagerie import MenagerieScene
from neura_runtime.binding import ExecutionBinding, load_binding
from neura_runtime.grpc_client import RobotCredentials
from neura_runtime.policy import PolicyClient, PolicyError


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


class MujocoArmConfig(_Model):
    type: Literal["mujoco"]
    # A MuJoCo Menagerie arm: franka_panda, ur5e, kinova_gen3, xarm7 or so_arm100. Its robot
    # profile is read from the model, with its front camera.
    model: str
    on_exit: Literal["hold"] = "hold"

    @model_validator(mode="after")
    def _known(self) -> MujocoArmConfig:
        try:
            from neura_robots.sim.menagerie import MODELS
        except ImportError as error:
            raise ValueError(f"a simulated arm needs the sim extra and MuJoCo: {error}") from error
        if self.model not in MODELS:
            raise ValueError(f"model {self.model!r}: one of {', '.join(MODELS)}")
        return self


class RecordedArmConfig(_Model):
    type: Literal["recorded"]
    # A LeRobot dataset of a real arm: a Hugging Face Hub name, or a local copy under `root`.
    dataset: str
    episode: int = Field(default=0, ge=0)
    root: Path | None = None
    # Each camera role of the robot profile, and the dataset's camera it is served from.
    cameras: dict[str, str] = Field(default_factory=dict)
    on_exit: Literal["hold"] = "hold"


class CameraEntry(_Model):
    # ros2: frames from the ROS 2 relay node, which subscribes to the camera's topic
    type: Literal["opencv", "synthetic", "ros2"] = "opencv"
    index_or_path: int | str | None = None
    fps: float = Field(default=30.0, gt=0)


class RobotConfig(_Model):
    robot_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    # Served by the site gateway (with credentials), or by `policy` directly.
    gateway: str | None = None
    credentials: CredentialsConfig | None = None
    policy: PolicyConfig | None = None
    # Contracts to serve; without them, whatever revision the site assigns this robot.
    revision: RevisionConfig | None = None
    arm: ArmConfig | Ros2ArmConfig | MujocoArmConfig | RecordedArmConfig
    cameras: dict[str, CameraEntry] = Field(default_factory=dict)
    instruction: str | None = None
    # Where the robot spools recorded episodes when its site records them (design §28.2); unset,
    # the robot never records, whatever the site asks.
    capture_dir: Path | None = None
    # Where incident records are written (a fault, or the operator's `incident`); null for none.
    incidents_dir: Path | None = Path("incidents")

    @model_validator(mode="after")
    def _one_source(self) -> RobotConfig:
        if (self.gateway is None) == (self.policy is None):
            raise ValueError("set gateway (and credentials) or policy, not both")
        if self.gateway is not None and self.credentials is None:
            raise ValueError("a gateway needs credentials")
        simulated = isinstance(self.arm, MujocoArmConfig)
        if self.policy is not None and self.revision is None and not simulated:
            raise ValueError("a policy without a gateway needs `revision:`, the robot's contracts")
        return self

    @model_validator(mode="after")
    def _cameras_match_the_arm(self) -> RobotConfig:
        if isinstance(self.arm, MujocoArmConfig | RecordedArmConfig) and self.cameras:
            raise ValueError("this arm's cameras are its own: leave the top-level `cameras:` out")
        ros2 = isinstance(self.arm, Ros2ArmConfig)
        wrong = sorted(role for role, c in self.cameras.items() if (c.type == "ros2") != ros2)
        if wrong:
            kind = "ros2: a ROS 2 robot's frames come" if ros2 else "not ros2: only a ROS 2 robot's"
            raise ValueError(f"cameras {', '.join(wrong)} must be {kind} through the relay")
        return self

    @classmethod
    def load(cls, path: Path, *, policy: str | None = None) -> RobotConfig:
        """``policy`` (a URI) runs that policy instead of the gateway, keeping any other
        ``policy:`` settings the file has."""
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if policy is not None:
            data.pop("gateway", None)
            data.pop("credentials", None)
            data["policy"] = {**(data.get("policy") or {}), "uri": policy}
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
        credentials = config.credentials and config.credentials.model_copy(
            update={"cert_dir": rel(config.credentials.cert_dir)}
        )
        arm: ArmConfig | Ros2ArmConfig | MujocoArmConfig | RecordedArmConfig = config.arm
        if isinstance(config.arm, Ros2ArmConfig):
            arm = config.arm.model_copy(update={"socket": rel(config.arm.socket)})
        elif isinstance(config.arm, RecordedArmConfig):
            arm = config.arm.model_copy(update={"root": rel(config.arm.root)})
        elif isinstance(config.arm, ArmConfig):
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
                "incidents_dir": rel(config.incidents_dir),
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

    def open_arm(self, profile: RobotProfile) -> So101Arm | Ros2Arm | SimArm | RecordedArm:
        """The configured arm, not yet connected."""
        arm = self.arm
        if isinstance(arm, MujocoArmConfig):
            return _simulated_arm(arm, profile)
        if isinstance(arm, RecordedArmConfig):
            return RecordedArm(
                RecordedEpisode(arm.dataset, arm.episode, arm.root), profile, arm.cameras
            )
        if not isinstance(arm, Ros2ArmConfig):
            return So101Arm(self.so101(), profile)
        joints = [Ros2Joint(j.ros, j.name, j.scale, j.offset) for j in arm.joints]
        config = Ros2Config(
            arm.socket, joints, list(self.cameras), arm.state_timeout_ms, arm.connect_timeout_s
        )
        return Ros2Arm(config, profile)

    def robot_credentials(self) -> RobotCredentials:
        c = self.credentials
        assert c is not None  # with a gateway, the validator requires credentials
        if c.cert_dir is not None:
            loaded = RobotCredentials.from_directory(c.cert_dir, self.robot_id)
            return dataclasses.replace(loaded, server_name=c.tls_server_name)
        return RobotCredentials(token=c.token)

    def binding(self, base: Path = Path(".")) -> ExecutionBinding | None:
        """The contracts: the configured revision's, or, for a simulated arm and a policy file,
        inferred from the arm's model and what the policy declares (``base``: where a relative
        policy file is read from)."""
        r = self.revision
        if r is not None:
            return load_binding(r.artifact, r.robot_profile, r.execution_profile)
        if isinstance(self.arm, MujocoArmConfig) and self.policy is not None:
            return inferred_binding(self.policy, _scene(self.arm.model).profile(), base)
        return None


CAMERA_FPS = 15.0  # a simulated arm's camera: rendering is the costly part of a simulation


@cache
def _scene(model: str) -> MenagerieScene:
    from neura_robots.sim.menagerie import MenagerieScene

    return MenagerieScene(model)


def _simulated_arm(config: MujocoArmConfig, profile: RobotProfile) -> SimArm:
    """The Menagerie arm on its own real-time clock, from its home pose."""
    from neura_robots.sim.arm import SimArm
    from neura_robots.sim.world import RealTimeWorld

    world = RealTimeWorld(_scene(config.model)).start()
    world.go()
    return SimArm(world, profile, camera_fps=CAMERA_FPS)


def _say(line: str) -> None:
    print(line, flush=True)


def _where(arm: So101Arm | Ros2Arm | SimArm | RecordedArm) -> str:
    if isinstance(arm, So101Arm):
        return arm.lock.device
    if isinstance(arm, Ros2Arm):
        return f"the relay on {arm.config.socket}"
    if isinstance(arm, RecordedArm):
        return f"a recording ({arm.episode.dataset.repo_id})"
    return f"MuJoCo ({arm.sim.model.nu} actuators)"


def check(config: RobotConfig, robot_profile: Path | None) -> int:
    if robot_profile is not None:
        profile = load_document(robot_profile.read_text(encoding="utf-8"))
        if not isinstance(profile, RobotProfile):
            raise SystemExit(f"{robot_profile} is not a robot profile")
    elif isinstance(config.arm, MujocoArmConfig):
        loaded = load_document(json.dumps(_scene(config.arm.model).profile()))
        assert isinstance(loaded, RobotProfile)
        profile = loaded
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


async def serve(
    config: RobotConfig, *, status_every_s: float | None = None, base: Path = Path(".")
) -> int:
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

    try:
        binding = config.binding(base)
    except PolicyError as exc:
        print(f"cannot serve: {exc}", file=sys.stderr)
        return 1
    credentials = config.robot_credentials() if config.gateway is not None else None
    arm: So101Arm | Ros2Arm | SimArm | RecordedArm | None = None
    policy: PolicyClient | None = None
    current: list[Any] = []

    async def report() -> None:
        while status_every_s and not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=status_every_s)
            process = current[0] if current else None
            if isinstance(process, AssignedArm):
                process = process.process
            if process is not None:
                print(json.dumps(process.status()), file=sys.stderr, flush=True)

    reporter = asyncio.create_task(report())
    try:
        if binding is not None:
            if config.policy is not None:  # checked before the arm is even connected
                policy = await open_policy(config.policy, binding, base)
                _say(f"{config.robot_id}: the policy is {describe(config.policy)}")
            arm = config.open_arm(binding.robot)
            arm.connect()
            process = ArmProcess(
                arm,
                binding,
                config.robot_id,
                config.revision.id if config.revision else "",
                config.gateway or "",
                credentials or "",
                inbox=inbox,
                instruction=config.instruction,
                say=_say,
                on_quit=stop.set,
                policy=policy,
                incidents=config.incidents_dir,
            )
            current.append(process)
            _say(f"{config.robot_id}: connected to the arm on {_where(arm)}; serving")
            await process.connection.run(stop)
        else:

            def open_arm(profile: RobotProfile) -> So101Arm | Ros2Arm | SimArm | RecordedArm:
                nonlocal arm
                arm = config.open_arm(profile)
                arm.connect()
                _say(f"{config.robot_id}: connected to the arm on {_where(arm)}")
                return arm

            # No revision: the site assigns one, so there is a gateway (the validator says so).
            assert config.gateway is not None
            assert credentials is not None
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
    except (ArmError, ActuatorBusyError, PolicyError) as exc:
        print(f"cannot serve: {exc}", file=sys.stderr)
        return 1
    finally:
        stop.set()
        await reporter
        if policy is not None:
            await policy.close()
        for running in current:
            if isinstance(running, ArmProcess) and running.incidents is not None:
                running.incidents.close()  # a record a fault is waiting on is written now
        if arm is not None:
            on_exit = config.arm.on_exit
            if isinstance(arm, RecordedArm):  # the commands against what was recorded
                _say(json.dumps(describe_recording(arm)))
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
    run_parser.add_argument(
        "--policy", metavar="URI", help="picker.py[:Class] or neura://host:port"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("NEURA_LOG_LEVEL", "warning").upper())
    policy = getattr(args, "policy", None)
    if policy and "://" not in policy:  # a file on the command line: from where it was typed
        path, sep, name = policy.partition(":")
        policy = str(Path(path).resolve()) + sep + name
    config = RobotConfig.load(args.config, policy=policy)
    if args.command == "check":
        return check(config, args.robot_profile)
    return asyncio.run(
        serve(config, status_every_s=args.status_every, base=args.config.parent.resolve())
    )


if __name__ == "__main__":
    sys.exit(main())
