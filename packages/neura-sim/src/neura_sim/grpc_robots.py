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
from typing import Any

import httpx

from neura_contracts import (
    ContractError,
    ExecutionProfile,
    ModelArtifact,
    RobotProfile,
    load_document,
    parse_document,
)
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
        self,
        binding: ExecutionBinding,
        robot_id: str,
        revision_id: str,
        target: str,
        token: str,
        *,
        arm: SimulatedArm | None = None,
        task_seconds: float | None = None,
        instruction: str = "pick and place",
    ) -> None:
        self.robot_id = robot_id
        self.revision_id = revision_id
        self.arm = arm or SimulatedArm(binding.robot)
        self.controller = SimController(self.arm)
        self.sensors = SimSensors(self.arm, binding.robot)
        self.runtime = RobotRuntime(binding, self.controller, self.sensors)
        if binding.language_input is not None:
            self.runtime.set_instruction(instruction[: binding.language_max_bytes])
        self.client = SessionClient(
            self.runtime, SessionConfig(robot_id=robot_id, revision_id=revision_id)
        )
        self.connection = GatewayConnection(self.client, target, token, on_tick=self._on_tick)
        self.task_seconds = task_seconds
        self.hold = False  # when True, finish the current task and do not start another
        self.tasks_completed = 0
        self._task_started_us: int | None = None

    def _on_tick(self, now_us: int) -> None:
        self.arm.step()
        state = self.runtime.state
        if (
            state is RuntimeState.RUNNING
            and self.task_seconds is not None
            and self._task_started_us is not None
            and now_us - self._task_started_us >= self.task_seconds * 1e6
        ):
            # A task boundary: the only point at which this robot may switch revisions.
            with contextlib.suppress(TransitionError):
                self.runtime.complete_task(now_us)
                self.tasks_completed += 1
            self._task_started_us = None
            return
        if state is RuntimeState.READY and self.runtime.session_id is not None and not self.hold:
            with contextlib.suppress(TransitionError):
                self.runtime.arm(now_us)
                self._task_started_us = now_us

    @property
    def at_task_boundary(self) -> bool:
        return self.runtime.state in (RuntimeState.READY, RuntimeState.DISCONNECTED)

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


class AssignedRobot:
    """A simulated robot that runs whatever revision the site agent assigns it, switching only at
    a task boundary (design §16): finish the task, close the session, load and verify the new
    contracts, open a session for the new revision."""

    def __init__(
        self,
        name: str,
        *,
        gateway: str,
        agent_url: str,
        token: str,
        task_seconds: float = 8.0,
        poll_seconds: float = 1.0,
    ) -> None:
        self.name = name
        self._gateway = gateway
        self._agent_url = agent_url.rstrip("/")
        self._token = token
        self._task_seconds = task_seconds
        self._poll = poll_seconds
        self.process: SimulatedRobotProcess | None = None
        self.arm: SimulatedArm | None = None
        self.switches = 0
        self.last_error: str | None = None

    async def _assignment(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        response = await client.get(
            f"{self._agent_url}/v1/local/robots/{self.name}/assignment",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    async def run(self, stop: asyncio.Event) -> None:
        connection_task: asyncio.Task[None] | None = None
        process_stop = asyncio.Event()
        async with httpx.AsyncClient(timeout=5) as client:
            while not stop.is_set():
                try:
                    assignment = await self._assignment(client)
                    self.last_error = None
                except (httpx.HTTPError, ValueError) as exc:
                    assignment = None
                    self.last_error = str(exc)
                wanted = assignment["revision_id"] if assignment else None
                current = self.process.revision_id if self.process else None
                if wanted is not None and wanted != current:
                    if self.process is not None:
                        self.process.hold = True
                    if self.process is None or self.process.at_task_boundary:
                        assert assignment is not None
                        try:
                            binding = binding_from_documents(assignment["documents"])
                        except (ValueError, KeyError, ContractError) as exc:
                            # Refuse the revision and keep running the current one.
                            self.last_error = f"refused revision {wanted}: {exc}"
                            if self.process is not None:
                                self.process.hold = False
                            await asyncio.sleep(self._poll)
                            continue
                        if connection_task is not None:
                            process_stop.set()
                            await connection_task
                        if self.arm is None:
                            self.arm = SimulatedArm(binding.robot)
                        self.process = SimulatedRobotProcess(
                            binding,
                            self.name,
                            wanted,
                            self._gateway,
                            self._token,
                            arm=self.arm,
                            task_seconds=self._task_seconds,
                        )
                        process_stop = asyncio.Event()
                        connection_task = asyncio.create_task(
                            self.process.connection.run(process_stop)
                        )
                        self.switches += 1
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll)
        if connection_task is not None:
            process_stop.set()
            await connection_task

    def status(self) -> dict[str, object]:
        if self.process is None:
            return {"robot": self.name, "state": "Unassigned", "error": self.last_error}
        return {
            **self.process.status(),
            "revision": self.process.revision_id,
            "switches": self.switches,
            "tasks": self.process.tasks_completed,
        }


def binding_from_documents(documents: dict[str, Any]) -> ExecutionBinding:
    """Parse and verify contracts received from the agent; refuses incompatible ones."""
    artifact = parse_document(documents["model_artifact"])
    robot = parse_document(documents["robot_profile"])
    profile = parse_document(documents["execution_profile"])
    if not (
        isinstance(artifact, ModelArtifact)
        and isinstance(robot, RobotProfile)
        and isinstance(profile, ExecutionProfile)
    ):
        raise ValueError("assignment documents have the wrong kinds")
    return ExecutionBinding(artifact, robot, profile)


async def run_robots(
    robots: list[SimulatedRobotProcess] | list[AssignedRobot],
    *,
    duration_s: float | None,
    report_every_s: float,
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    tasks = [
        asyncio.create_task(r.run(stop) if isinstance(r, AssignedRobot) else r.connection.run(stop))
        for r in robots
    ]

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
    parser.add_argument(
        "--agent-url",
        default=env("NEURA_SIM_AGENT_URL"),
        help="site agent's local API; robots then run the revision the agent assigns them and "
        "switch at task boundaries (otherwise --revision with the contracts given here)",
    )
    parser.add_argument(
        "--task-seconds",
        type=float,
        default=float(env("NEURA_SIM_TASK_SECONDS", "5")),
        help="length of one simulated task; revisions switch between tasks",
    )
    args = parser.parse_args()
    logging.basicConfig(level=env("NEURA_LOG_LEVEL", "warning").upper())
    if args.agent_url:
        assigned = [
            AssignedRobot(
                f"{args.robot_prefix}{i}",
                gateway=args.gateway,
                agent_url=args.agent_url,
                token=args.token_template.format(robot_id=f"{args.robot_prefix}{i}"),
                task_seconds=args.task_seconds,
            )
            for i in range(args.robots)
        ]
        asyncio.run(
            run_robots(assigned, duration_s=args.duration, report_every_s=args.report_every)
        )
        return
    binding = load_binding(args.artifact, args.robot_profile, args.profile)
    robots = []
    for i in range(args.robots):
        robot_id = f"{args.robot_prefix}{i}"
        token = args.token_template.format(robot_id=robot_id)
        robots.append(SimulatedRobotProcess(binding, robot_id, args.revision, args.gateway, token))
    asyncio.run(run_robots(robots, duration_s=args.duration, report_every_s=args.report_every))


if __name__ == "__main__":
    main()
