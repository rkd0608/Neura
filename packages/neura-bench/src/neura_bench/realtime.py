"""Real-time runs: the production gateway with real policy worker processes on the device, and
simulated robots running the production robot runtime over gRPC (design §10).

Everything a robot sees is real: its session, leases, the gateway's scheduler, the worker
processes and their forward passes, serialization and the loopback network. The robots' arms and
cameras are simulated. Robots and the gateway share one process and one monotonic clock, which is
what lets observation age be measured exactly; the harness reports its own lateness (robot ticks
it could not run on time) so an overloaded harness is visible rather than mistaken for the
system under test.

Admission control is set aside, as in simulation: the benchmark measures what N robots get.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neura.v1 import dataplane_pb2
from neura_bench.deployments import DeploymentFiles
from neura_bench.measure import WORKER, background_command
from neura_bench.metrics import Result, Snapshot, snapshot, summarize, window
from neura_bench.sim import AdmitAll, LatencyModel
from neura_runtime.policy import ModelInputs
from neura_runtime.runtime import RuntimeState, TransitionError
from neura_server.backends import WorkerBackend
from neura_server.config import (
    DeploymentConfig,
    DeviceConfig,
    GatewayConfig,
    ServingConfig,
    token_digest,
)
from neura_server.grpc_server import GatewayServer
from neura_server.robot_service import monotonic_us
from neura_server.scheduler import Batch, Policy
from neura_sim.grpc_robots import SimulatedRobotProcess


@dataclass(frozen=True, slots=True)
class RealGroup:
    name: str
    files: DeploymentFiles
    robots: tuple[str, ...]
    latency: LatencyModel  # measured; the gateway's scheduler starts from it


@dataclass(frozen=True, slots=True)
class RealScenario:
    groups: tuple[RealGroup, ...]
    device: str
    policy: Policy = Policy.DEADLINE_FAIRNESS
    max_batch: int = 8
    starts_s: dict[str, float] = field(default_factory=dict)
    warmup_s: float = 5.0
    duration_s: float = 30.0
    background: bool = False
    reset_after_s: float = 1.0


@dataclass(frozen=True, slots=True)
class RealResult:
    result: Result
    late_tick_fraction: float  # robot control ticks the harness ran late
    worker_restarts: int


def workers(server: GatewayServer) -> list[WorkerBackend]:
    backends = list(server.backends().values())
    assert all(isinstance(b, WorkerBackend) for b in backends)
    return [b for b in backends if isinstance(b, WorkerBackend)]


def _profile_file(directory: Path, name: str, latency: LatencyModel) -> Path:
    path = directory / f"{name}.profile.json"
    path.write_text(
        json.dumps(
            {
                "points": [
                    {
                        "batch_size": p.batch_size,
                        "p50_us": p.p50_us,
                        "p99_us": p.p99_us,
                        "memory_mib": p.memory_mib,
                    }
                    for p in latency.points
                ]
            }
        )
    )
    return path


class _Instruments:
    """Measurement hooks on a running gateway."""

    def __init__(self, server: GatewayServer, device: str) -> None:
        self.captured: dict[tuple[str, int, int], int] = {}
        self.ages: list[int] = []
        self.served_late = 0
        self.measuring = False
        self.device = device
        self._running = 0
        self._busy_since: int | None = None
        self.busy_us = 0
        core = server.core
        receive = core.receive

        def recording_receive(
            robot_id: str, message: dataplane_pb2.RobotMessage, size: int, now_us: int
        ) -> Any:
            if message.HasField("observation"):
                observation = message.observation
                env = observation.envelope
                key = (env.session_id, env.task_epoch, env.sequence)
                self.captured[key] = int(observation.capture_time_us)
            return receive(robot_id, message, size, now_us)

        core.receive = recording_receive  # type: ignore[method-assign, assignment]
        for batch_group in core.groups.values():
            scheduler = batch_group.scheduler
            batch_group.admission = AdmitAll(scheduler.profile, max_batch=scheduler.max_batch)
            scheduler.on_dispatch = self._on_dispatch
            scheduler.on_complete = self._on_complete
        for backend in workers(server):
            infer = backend.infer

            async def timed(
                batch_id: int,
                batch: Sequence[ModelInputs],
                *,
                timeout_s: float,
                _infer: Any = infer,
            ) -> Any:
                self._start()
                try:
                    return await _infer(batch_id, batch, timeout_s=timeout_s)
                finally:
                    self._stop()

            backend.infer = timed  # type: ignore[method-assign]

    def _start(self) -> None:
        if self._running == 0:
            self._busy_since = monotonic_us()
        self._running += 1

    def _stop(self) -> None:
        self._running -= 1
        if self._running == 0 and self._busy_since is not None:
            self.busy_us += monotonic_us() - self._busy_since
            self._busy_since = None

    def busy_s(self) -> float:
        busy = self.busy_us
        if self._busy_since is not None:
            busy += monotonic_us() - self._busy_since
        return busy / 1e6

    def _on_dispatch(self, batch: Batch) -> None:
        now = monotonic_us()
        for request in batch.requests:
            env = request.envelope
            captured = self.captured.pop((env.session_id, env.task_epoch, env.sequence), None)
            if self.measuring and captured is not None:
                self.ages.append(now - captured)

    def _on_complete(self, batch: Batch, now_us: int) -> None:
        if not self.measuring:
            return
        for request in batch.requests:
            if request.deadline_us > request.received_at_us and now_us > request.deadline_us:
                self.served_late += 1


async def run(scenario: RealScenario) -> RealResult:
    names = [name for g in scenario.groups for name in g.robots]
    with tempfile.TemporaryDirectory(prefix="neura-rt-") as tmp:
        directory = Path(tmp)
        config = GatewayConfig(
            listen="127.0.0.1:0",
            status_listen=None,
            devices=[
                DeviceConfig(robot_id=n, token_sha256=token_digest(f"token-{n}")) for n in names
            ],
            deployments=[
                DeploymentConfig(
                    revision_id=f"rev_{g.name}",
                    artifact=g.files.artifact,
                    robot_profile=g.files.robot_profile,
                    execution_profile=g.files.execution_profile,
                    robots=list(g.robots),
                    checkpoint=g.files.checkpoint,
                    device=scenario.device,
                    profile=_profile_file(directory, g.name, g.latency),
                )
                for g in scenario.groups
            ],
            serving=ServingConfig(
                backend="workers",
                policy=scenario.policy,
                max_batch=scenario.max_batch,
                worker_command=WORKER,
                worker_start_timeout_s=900,
                worker_timeout_ms=5000,
            ),
        )
        server = GatewayServer(config)
        instruments = _Instruments(server, scenario.device)
        await server.start()
        try:
            return await _drive(server, instruments, scenario)
        finally:
            await server.stop()


async def _drive(
    server: GatewayServer, instruments: _Instruments, scenario: RealScenario
) -> RealResult:
    for backend in workers(server):
        await asyncio.wait_for(backend.ready.wait(), timeout=900)
    target = f"127.0.0.1:{server.bound_port}"
    robots: dict[str, SimulatedRobotProcess] = {}
    for group in scenario.groups:
        for name in group.robots:
            robots[name] = SimulatedRobotProcess(
                group.files.binding, name, f"rev_{group.name}", target, f"token-{name}"
            )
    faults: Counter[str] = Counter()
    stop = asyncio.Event()

    async def robot(name: str) -> None:
        await asyncio.sleep(scenario.starts_s.get(name, 0.0))
        await robots[name].connection.run(stop)

    async def operator() -> None:
        """Resets a robot that latched a fault after reset_after_s, as a site operator would."""
        faulted_at: dict[str, float] = {}
        while not stop.is_set():
            for name, process in robots.items():
                if process.runtime.state is RuntimeState.FAULTED:
                    if name not in faulted_at:
                        faulted_at[name] = time.monotonic()
                        faults[name] += 1
                    elif time.monotonic() - faulted_at[name] >= scenario.reset_after_s:
                        with contextlib.suppress(TransitionError):
                            process.runtime.reset(monotonic_us(), revalidated=True)
                        del faulted_at[name]
            await asyncio.sleep(0.02)

    def take() -> Snapshot:
        return snapshot(
            (g.scheduler.stats for g in server.core.groups.values()),
            {name: (process.runtime, faults[name]) for name, process in robots.items()},
            {scenario.device: instruments.busy_s()},
        )

    tasks = [asyncio.create_task(robot(name)) for name in robots]
    tasks.append(asyncio.create_task(operator()))
    background: asyncio.subprocess.Process | None = None
    try:
        if scenario.background:
            background = await asyncio.create_subprocess_exec(
                *background_command(scenario.device),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
            )
        await asyncio.sleep(scenario.warmup_s)
        before = take()
        ticks_before = sum(r.connection.ticks for r in robots.values())
        late_before = sum(r.connection.late_ticks for r in robots.values())
        instruments.measuring = True
        started = time.monotonic()
        await asyncio.sleep(scenario.duration_s)
        elapsed = time.monotonic() - started
        instruments.measuring = False
        after = take()
        ticks = sum(r.connection.ticks for r in robots.values()) - ticks_before
        late = sum(r.connection.late_ticks for r in robots.values()) - late_before
    finally:
        stop.set()
        if background is not None:
            background.terminate()
            await background.wait()
        await asyncio.gather(*tasks, return_exceptions=True)
    memory = 0
    restarts = 0
    for backend in workers(server):
        memory += backend.peak_memory_mib
        restarts += backend.restarts
    result = summarize(
        window(
            before,
            after,
            duration_s=elapsed,
            devices=1,
            served_late=instruments.served_late,
            ages_at_dispatch_us=instruments.ages,
            peak_memory_mib=memory,
        )
    )
    return RealResult(result, round(late / max(ticks, 1), 4), restarts)
