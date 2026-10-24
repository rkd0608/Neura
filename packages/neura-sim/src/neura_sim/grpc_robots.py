"""Run simulated robots against a live gateway over gRPC: ``neura-sim-robots``.

Each robot is a simulated arm with the same runtime, session client and gRPC connection a real
integration uses. Robots arm automatically whenever they are Ready (a stand-in for an operator's
local approval) and print a status line every few seconds. With ``--check-invariants`` every
robot is watched by the independent InvariantMonitor (design §21: any violation is a Sev-1), and
``--reset-after`` stands in for an operator who resets a latched fault after a while.
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
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import grpc

from neura.v1 import dataplane_pb2
from neura_contracts import (
    ContractError,
    ExecutionProfile,
    ModelArtifact,
    RobotProfile,
    load_document,
    parse_document,
)
from neura_runtime.binding import ExecutionBinding
from neura_runtime.grpc_client import GatewayConnection, RobotCredentials, fetch_assignment
from neura_runtime.runtime import RobotRuntime, RuntimeState, TransitionError
from neura_runtime.session import Outbound, SessionClient, SessionConfig
from neura_sim.arm import SimController, SimSensors, SimulatedArm
from neura_sim.invariants import InvariantMonitor


def load_binding(artifact: Path, robot: Path, profile: Path) -> ExecutionBinding:
    a = load_document(artifact.read_text(encoding="utf-8"))
    r = load_document(robot.read_text(encoding="utf-8"))
    p = load_document(profile.read_text(encoding="utf-8"))
    assert isinstance(a, ModelArtifact)
    assert isinstance(r, RobotProfile)
    assert isinstance(p, ExecutionProfile)
    return ExecutionBinding(a, r, p)


def watch_robot(
    monitor: InvariantMonitor,
    owner: object,
    robot_id: str,
    client: SessionClient,
    last_command: Callable[[], Any],
) -> Any:
    """Wire a robot's session client and runtime to ``monitor``: every observation it sends,
    session it is granted, chunk it receives and action it dispatches. ``last_command`` returns
    the command vector the controller was last given. Returns the monitor's record."""
    runtime = client.runtime
    record = monitor.client(owner, robot_id, runtime.binding)
    drain = client.drain
    on_open = client.on_open_response
    on_message = client.on_gateway_message

    def watched_drain() -> list[Outbound]:
        items = drain()  # SessionClient.tick drains through this too
        for item in items:
            message = item.message
            if (
                isinstance(message, dataplane_pb2.RobotMessage)
                and message.WhichOneof("body") == "observation"
            ):
                monitor.observation_sent(record, message.observation)
        return items

    def watched_open(response: dataplane_pb2.OpenSessionResponse, now_us: int) -> None:
        if response.WhichOneof("result") == "accepted":
            accepted = response.accepted
            monitor.session_granted(record, accepted.session_id, accepted.lease_generation)
        on_open(response, now_us)

    def watched_message(message: dataplane_pb2.GatewayMessage, now_us: int) -> None:
        if message.WhichOneof("body") != "action_chunk":
            on_message(message, now_us)
            return
        arrival = monitor.chunk_arriving(
            record,
            message.action_chunk,
            session_id=runtime.session_id,
            lease_generation=runtime.lease_generation,
            task_epoch=runtime.task_epoch,
            state=runtime.state,
            accepted_before=runtime.counters["chunks_accepted"],
            now_us=now_us,
        )
        on_message(message, now_us)
        monitor.chunk_handled(arrival, runtime.counters["chunks_accepted"], runtime.state)

    client.drain = watched_drain  # type: ignore[method-assign]
    client.on_open_response = watched_open  # type: ignore[method-assign]
    client.on_gateway_message = watched_message  # type: ignore[method-assign]
    runtime.dispatch_listener = lambda epoch, index: monitor.dispatched(
        record, epoch, index, last_command(), runtime.state
    )
    return record


class SimulatedRobotProcess:
    def __init__(
        self,
        binding: ExecutionBinding,
        robot_id: str,
        revision_id: str,
        target: str,
        credentials: RobotCredentials | str,
        *,
        arm: SimulatedArm | None = None,
        task_seconds: float | None = None,
        instruction: str = "pick and place",
        reset_after_s: float | None = None,
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
        self.connection = GatewayConnection(self.client, target, credentials, on_tick=self._on_tick)
        self.task_seconds = task_seconds
        self.hold = False  # when True, finish the current task and do not start another
        self.tasks_completed = 0
        self.resets = 0
        self.reset_after_s = reset_after_s
        self._task_started_us: int | None = None
        self._monitor: InvariantMonitor | None = None
        self._watched: Any = None

    def watch(self, monitor: InvariantMonitor) -> None:
        """Report everything this robot sends, receives and does to ``monitor``."""
        controller = self.controller
        self._monitor = monitor
        self._watched = watch_robot(
            monitor, self, self.robot_id, self.client, lambda: controller.last_command
        )

    @property
    def violations(self) -> int:
        if self._monitor is None:
            return 0
        return self._monitor.by_client[self._watched.label]

    def reset(self, now_us: int) -> None:
        """The explicit local reset of a latched fault, after revalidation (design §7)."""
        if self._monitor is not None:
            self._monitor.reset(self._watched)
        self.runtime.reset(now_us, revalidated=True)
        self.resets += 1

    def _on_tick(self, now_us: int) -> None:
        self.arm.step()
        state = self.runtime.state
        if self._monitor is not None:
            runtime = self.runtime
            self._monitor.ticked(self._watched, now_us, state, runtime.task_epoch, runtime.fault)
        fault = self.runtime.fault
        if (
            state is RuntimeState.FAULTED
            and fault is not None
            and self.reset_after_s is not None
            and now_us - fault.at_us >= self.reset_after_s * 1e6
        ):
            with contextlib.suppress(TransitionError):
                self.reset(now_us)
            return
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
            "resets": self.resets,
            **({"violations": self.violations} if self._monitor is not None else {}),
        }


class AssignedRobot:
    """A simulated robot that runs whatever revision the site assigns it, switching only at a
    task boundary (design §16): finish the task, close the session, load and verify the new
    contracts, open a session for the new revision. It asks the gateway for its assignment,
    over the same authenticated channel it is served on."""

    def __init__(
        self,
        name: str,
        *,
        gateway: str,
        credentials: RobotCredentials | str | Callable[[], RobotCredentials],
        task_seconds: float = 8.0,
        poll_seconds: float = 1.0,
        monitor: InvariantMonitor | None = None,
        reset_after_s: float | None = None,
    ) -> None:
        self.name = name
        self._monitor = monitor
        self._reset_after_s = reset_after_s
        self._gateway = gateway
        # A callable loads credentials when first needed (a robot can start before the site
        # has issued its certificate) and is retried until it succeeds.
        self._credentials_source = credentials
        self._credentials: RobotCredentials | str | None = (
            None if callable(credentials) else credentials
        )
        self._task_seconds = task_seconds
        self._poll = poll_seconds
        self.process: SimulatedRobotProcess | None = None
        self.arm: SimulatedArm | None = None
        self.switches = 0
        self.last_error: str | None = None

    def _resolved(self) -> RobotCredentials | str:
        if self._credentials is None:
            source = self._credentials_source
            assert callable(source)
            self._credentials = source()
        return self._credentials

    async def _assignment(self) -> dict[str, Any] | None:
        assignment = await fetch_assignment(self._gateway, self._resolved())
        if not assignment.revision_id:
            return None
        return {
            "revision_id": assignment.revision_id,
            "documents": {role: json.loads(raw) for role, raw in assignment.documents.items()},
        }

    async def run(self, stop: asyncio.Event) -> None:
        connection_task: asyncio.Task[None] | None = None
        process_stop = asyncio.Event()
        while not stop.is_set():
            try:
                assignment = await self._assignment()
                self.last_error = None
            except (grpc.aio.AioRpcError, ValueError, OSError) as exc:
                assignment = None  # not enrolled yet, gateway not up, or not assigned
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
                        self._resolved(),
                        arm=self.arm,
                        task_seconds=self._task_seconds,
                        reset_after_s=self._reset_after_s,
                    )
                    if self._monitor is not None:
                        self.process.watch(self._monitor)
                    process_stop = asyncio.Event()
                    connection_task = asyncio.create_task(self.process.connection.run(process_stop))
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
    parser.add_argument(
        "--names",
        default=env("NEURA_SIM_NAMES", ""),
        help="comma-separated robot names, instead of --robot-prefix and --robots",
    )
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
        "--cert-dir",
        type=Path,
        default=env("NEURA_SIM_CERT_DIR") or None,
        help="mutual TLS: <robot>.key, <robot>.pem and ca.pem from the site CA",
    )
    parser.add_argument(
        "--tls-server-name",
        default=env("NEURA_SIM_TLS_SERVER_NAME"),
        help="the gateway name to verify, when --gateway is an address its certificate omits",
    )
    parser.add_argument(
        "--token-template",
        default=env("NEURA_SIM_TOKEN_TEMPLATE", "dev-token-{robot_id}"),
        help="device token per robot without TLS; {robot_id} is substituted (development only)",
    )
    parser.add_argument(
        "--duration", type=float, default=None, help="seconds to run (default: forever)"
    )
    parser.add_argument("--report-every", type=float, default=5.0)
    parser.add_argument(
        "--assigned",
        action="store_true",
        default=env("NEURA_SIM_ASSIGNED", "") in ("1", "true"),
        help="run the revision the site assigns each robot and switch at task boundaries "
        "(otherwise --revision with the contracts given here)",
    )
    parser.add_argument(
        "--check-invariants",
        action="store_true",
        default=env("NEURA_SIM_CHECK_INVARIANTS", "") in ("1", "true"),
        help="watch every robot with the independent invariant monitor; exit 1 on a violation",
    )
    parser.add_argument(
        "--reset-after",
        type=float,
        default=float(env("NEURA_SIM_RESET_AFTER", "0")) or None,
        help="reset a latched fault after this many seconds, as an operator would "
        "(default: never; a faulted robot stays faulted)",
    )
    parser.add_argument(
        "--task-seconds",
        type=float,
        default=float(env("NEURA_SIM_TASK_SECONDS", "5")),
        help="length of one simulated task; revisions switch between tasks",
    )
    args = parser.parse_args()
    logging.basicConfig(level=env("NEURA_LOG_LEVEL", "warning").upper())

    def credentials(robot_id: str) -> RobotCredentials:
        if args.cert_dir is not None:
            loaded = RobotCredentials.from_directory(args.cert_dir, robot_id)
            return dataclasses.replace(loaded, server_name=args.tls_server_name)
        return RobotCredentials(token=args.token_template.format(robot_id=robot_id))

    def later(robot_id: str) -> Callable[[], RobotCredentials]:
        return lambda: credentials(robot_id)

    monitor = (
        InvariantMonitor(clock=lambda: time.monotonic_ns() // 1000, gateway_observed=False)
        if args.check_invariants
        else None
    )

    names = [n for n in args.names.split(",") if n] or [
        f"{args.robot_prefix}{i}" for i in range(args.robots)
    ]
    if args.assigned:
        assigned = [
            AssignedRobot(
                name,
                gateway=args.gateway,
                credentials=later(name),
                task_seconds=args.task_seconds,
                monitor=monitor,
                reset_after_s=args.reset_after,
            )
            for name in names
        ]
        asyncio.run(
            run_robots(assigned, duration_s=args.duration, report_every_s=args.report_every)
        )
    else:
        binding = load_binding(args.artifact, args.robot_profile, args.profile)
        robots = []
        for robot_id in names:
            process = SimulatedRobotProcess(
                binding,
                robot_id,
                args.revision,
                args.gateway,
                credentials(robot_id),
                reset_after_s=args.reset_after,
            )
            if monitor is not None:
                process.watch(monitor)
            robots.append(process)
        asyncio.run(run_robots(robots, duration_s=args.duration, report_every_s=args.report_every))
    if monitor is not None:
        print(json.dumps({"invariants": monitor.summary()}), flush=True)
        if monitor.violations:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
