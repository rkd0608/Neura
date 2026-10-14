"""Run simulated robots against a live gateway over gRPC: ``neura-sim-robots``.

Each robot is a simulated arm with the same runtime, session client and gRPC connection a real
integration uses. Robots arm automatically whenever they are Ready (a stand-in for an operator's
local approval) and print a status line every few seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
from pathlib import Path

from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, load_document
from neura_runtime.binding import ExecutionBinding
from neura_runtime.grpc_client import GatewayConnection
from neura_runtime.runtime import RobotRuntime, RuntimeState, TransitionError
from neura_runtime.session import SessionClient, SessionConfig
from neura_sim.arm import SimController, SimSensors, SimulatedArm


def load_binding(artifact: Path, robot: Path, profile: Path) -> ExecutionBinding:
    a = load_document(artifact.read_text(encoding="utf-8"))
    r = load_document(robot.read_text(encoding="utf-8"))
    p = load_document(profile.read_text(encoding="utf-8"))
    assert isinstance(a, ModelArtifact)
    assert isinstance(r, RobotProfile)
    assert isinstance(p, ExecutionProfile)
    return ExecutionBinding(a, r, p)


class SimulatedRobotProcess:
    def __init__(
        self, binding: ExecutionBinding, robot_id: str, revision_id: str, target: str, token: str
    ) -> None:
        self.robot_id = robot_id
        self.arm = SimulatedArm(binding.robot)
        self.controller = SimController(self.arm)
        self.sensors = SimSensors(self.arm, binding.robot)
        self.runtime = RobotRuntime(binding, self.controller, self.sensors)
        self.client = SessionClient(
            self.runtime, SessionConfig(robot_id=robot_id, revision_id=revision_id)
        )
        self.connection = GatewayConnection(self.client, target, token, on_tick=self._on_tick)

    def _on_tick(self, now_us: int) -> None:
        self.arm.step()
        if self.runtime.state is RuntimeState.READY and self.runtime.session_id is not None:
            with contextlib.suppress(TransitionError):
                self.runtime.arm(now_us)

    def status(self) -> dict[str, object]:
        snapshot = self.runtime.snapshot()
        return {
            "robot": self.robot_id,
            "state": snapshot.state.value,
            "epoch": snapshot.task_epoch,
            "dispatched": self.runtime.dispatched,
            "queued": snapshot.queued_actions,
            "fault": snapshot.fault.reason.value if snapshot.fault else None,
            "late_ticks": self.connection.late_ticks,
            "rtt_p99_us": self.client.rtt.p99_us(),
        }


async def run_robots(
    robots: list[SimulatedRobotProcess], *, duration_s: float | None, report_every_s: float
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(r.connection.run(stop)) for r in robots]

    async def report() -> None:
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=report_every_s)
            print(json.dumps([r.status() for r in robots]), flush=True)

    reporter = asyncio.create_task(report())
    if duration_s is not None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=duration_s)
        stop.set()
    await asyncio.gather(*tasks, reporter)


def main() -> None:
    parser = argparse.ArgumentParser(prog="neura-sim-robots", description=__doc__)
    env = os.environ.get
    parser.add_argument("--gateway", default=env("NEURA_SIM_GATEWAY", "127.0.0.1:50051"))
    parser.add_argument("--robots", type=int, default=int(env("NEURA_SIM_ROBOTS", "4")))
    parser.add_argument("--robot-prefix", default=env("NEURA_SIM_ROBOT_PREFIX", "robot-"))
    parser.add_argument("--revision", default=env("NEURA_SIM_REVISION", "rev_dev"))
    parser.add_argument(
        "--artifact",
        type=Path,
        default=Path(env("NEURA_SIM_ARTIFACT", "examples/contracts/act-so101.artifact.yaml")),
    )
    parser.add_argument(
        "--robot-profile",
        type=Path,
        default=Path(env("NEURA_SIM_ROBOT_PROFILE", "examples/contracts/so101.robot.yaml")),
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(
            env("NEURA_SIM_EXECUTION_PROFILE", "examples/contracts/act-so101-30hz.profile.yaml")
        ),
    )
    parser.add_argument(
        "--token-template",
        default=env("NEURA_SIM_TOKEN_TEMPLATE", "dev-token-{robot_id}"),
        help="device token per robot; {robot_id} is substituted (development only)",
    )
    parser.add_argument(
        "--duration", type=float, default=None, help="seconds to run (default: forever)"
    )
    parser.add_argument("--report-every", type=float, default=5.0)
    args = parser.parse_args()
    logging.basicConfig(level=env("NEURA_LOG_LEVEL", "warning").upper())
    binding = load_binding(args.artifact, args.robot_profile, args.profile)
    robots = []
    for i in range(args.robots):
        robot_id = f"{args.robot_prefix}{i}"
        token = args.token_template.format(robot_id=robot_id)
        robots.append(SimulatedRobotProcess(binding, robot_id, args.revision, args.gateway, token))
    asyncio.run(run_robots(robots, duration_s=args.duration, report_every_s=args.report_every))


if __name__ == "__main__":
    main()
