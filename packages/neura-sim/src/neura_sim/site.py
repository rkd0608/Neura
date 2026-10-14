"""Deterministic discrete-event simulation of a Neura site (design §7, §16 step 3).

True time is virtual and advances only through events. Each robot and the gateway read their own
clock: ``true time + offset``, with offsets that can differ by days. The same sans-IO cores that run
in production (``SessionClient``/``RobotRuntime`` on the robot, ``GatewayCore``/``Scheduler`` on the
server) are driven here; only the transport and the GPU are simulated.
"""

from __future__ import annotations

import heapq
import itertools
import math
import random
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura.v1 import dataplane_pb2
from neura_contracts import ReasonCode
from neura_runtime.binding import ExecutionBinding
from neura_runtime.runtime import RobotRuntime, RuntimeConfig, RuntimeState
from neura_runtime.session import Outbound, SessionClient, SessionConfig
from neura_server.admission import AdmissionController
from neura_server.gateway import BatchGroup, Deployment, GatewayCore
from neura_server.leases import GenerationStore, LeaseTable, MemoryGenerationStore
from neura_server.policy import MockPolicy, PolicyBackend, PolicyError
from neura_server.profiles import LatencyProfile, ProfilePoint
from neura_server.scheduler import Batch, Policy, Scheduler
from neura_sim.arm import SimController, SimSensors, SimulatedArm
from neura_sim.network import Link, LinkModel

MS = 1_000
SECOND = 1_000_000


class EventLoop:
    def __init__(self) -> None:
        self.now_us = 0
        self._queue: list[tuple[int, int, Callable[[], None]]] = []
        self._order = itertools.count()

    def at(self, time_us: int, action: Callable[[], None]) -> None:
        heapq.heappush(self._queue, (max(time_us, self.now_us), next(self._order), action))

    def run_until(self, end_us: int) -> None:
        while self._queue and self._queue[0][0] <= end_us:
            time_us, _, action = heapq.heappop(self._queue)
            self.now_us = time_us
            action()
        self.now_us = max(self.now_us, end_us)


@dataclass(slots=True)
class WorkerModel:
    """Simulated GPU latency: lognormal around the profile's p50, scaled to roughly hit its p99."""

    profile: LatencyProfile
    tail_sigma: float = 0.35

    def sample_us(self, batch_size: int, rng: random.Random) -> int:
        p50 = self.profile.p50_us(batch_size)
        p99 = self.profile.p99_us(batch_size)
        sigma = min(self.tail_sigma, math.log(max(p99, p50 + 1) / p50) / 2.326)
        return max(1, int(p50 * math.exp(rng.gauss(0.0, sigma))))


@dataclass(slots=True)
class WorkerFaults:
    crash_at_us: list[int] = field(default_factory=list)  # true time
    restart_us: int = 500 * MS
    out_of_memory_batches: set[int] = field(default_factory=set)
    invalid_output_batches: set[int] = field(default_factory=set)
    corrupt_output_batches: set[int] = field(default_factory=set)  # out-of-range actions


@dataclass(slots=True)
class SiteConfig:
    binding: ExecutionBinding
    robots: int = 4
    policy: Policy = Policy.DEADLINE_FAIRNESS
    max_batch: int = 8
    profile: list[ProfilePoint] = field(
        default_factory=lambda: [
            ProfilePoint(1, 18 * MS, 25 * MS, 900),
            ProfilePoint(4, 24 * MS, 34 * MS, 1100),
            ProfilePoint(8, 32 * MS, 45 * MS, 1400),
        ]
    )
    reserve_fraction: float = 0.2
    lease_ttl_us: int = 2 * SECOND
    uplink: LinkModel = field(default_factory=LinkModel)
    downlink: LinkModel = field(default_factory=LinkModel)
    rpc_timeout_us: int = 500 * MS
    gateway_clock_offset_us: int = 7_777_000_000
    robot_clock_offsets_us: Sequence[int] | None = None
    auto_arm: bool = True
    seed: int = 1
    worker_faults: WorkerFaults = field(default_factory=WorkerFaults)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    policy_factory: Callable[[ExecutionBinding], PolicyBackend] | None = None
    revision_id: str = "rev_sim"


class SimRobot:
    def __init__(self, site: SimSite, name: str, offset_us: int) -> None:
        binding = site.config.binding
        self.site = site
        self.name = name
        self.offset_us = offset_us
        self.arm = SimulatedArm(binding.robot)
        self.controller = SimController(self.arm)
        self.sensors = SimSensors(self.arm, binding.robot, image_bytes=site.frames)
        self.runtime = RobotRuntime(binding, self.controller, self.sensors, site.config.runtime)
        self.client = SessionClient(
            self.runtime, SessionConfig(robot_id=name, revision_id=site.config.revision_id)
        )
        self.uplink = Link(site.config.uplink, site.rng)
        self.downlink = Link(site.config.downlink, site.rng)
        self.auto_arm = site.config.auto_arm
        self.dispatches: list[tuple[int, int]] = []
        self.states: Counter[RuntimeState] = Counter()
        self._calls: dict[int, str] = {}
        self._call_ids = itertools.count(1)
        self.runtime.dispatch_listener = lambda epoch, index: self.dispatches.append((epoch, index))

    def now(self) -> int:
        return self.site.loop.now_us + self.offset_us

    def tick(self) -> None:
        now = self.now()
        outbound = self.client.tick(now)
        self.arm.step()
        if (
            self.auto_arm
            and self.runtime.state is RuntimeState.READY
            and self.runtime.session_id is not None
        ):
            self.runtime.arm(now)
            outbound.extend(self.client.drain())
        self.states[self.runtime.state] += 1
        for item in outbound:
            self._send(item)
        self.site.loop.at(
            self.site.loop.now_us + self.site.config.binding.control_period_us, self.tick
        )

    def _send(self, item: Outbound) -> None:
        call_id = next(self._call_ids)
        if item.channel != "stream":
            self._calls[call_id] = item.channel
            timeout_at = self.site.loop.now_us + self.site.config.rpc_timeout_us
            self.site.loop.at(timeout_at, lambda: self._rpc_timeout(call_id))
        for at in self.uplink.deliveries(self.site.loop.now_us):
            self.site.loop.at(at, lambda: self.site.gateway_receive(self, item, call_id))

    def _rpc_timeout(self, call_id: int) -> None:
        channel = self._calls.pop(call_id, None)
        if channel == "open":
            self.client.on_open_failed(self.now())
        elif channel == "heartbeat":
            self.client.on_heartbeat_failed(self.now())

    def deliver_response(self, call_id: int, response: Any) -> None:
        channel = self._calls.pop(call_id, None)
        if channel is None:
            return  # timed out already, or a duplicate
        now = self.now()
        if channel == "open":
            self.client.on_open_response(response, now)
        elif channel == "heartbeat":
            self.client.on_heartbeat_response(response, now)
        elif channel == "control":
            self.client.on_control_response(response, now)

    def deliver_stream(self, message: dataplane_pb2.GatewayMessage) -> None:
        self.client.on_gateway_message(message, self.now())


class SimSite:
    def __init__(
        self, config: SiteConfig, *, generation_store: GenerationStore | None = None
    ) -> None:
        self.config = config
        self.loop = EventLoop()
        self.rng = random.Random(config.seed)
        binding = config.binding
        self.frames = {c.role: bytes(c.width * c.height * 3) for c in binding.robot.spec.cameras}
        self.store = generation_store or MemoryGenerationStore()
        self.profile = LatencyProfile(list(config.profile))
        self.worker_model = WorkerModel(self.profile)
        self.policy: PolicyBackend = (
            config.policy_factory(binding) if config.policy_factory else MockPolicy(binding)
        )
        offsets = list(config.robot_clock_offsets_us or [])
        self.robots = [
            SimRobot(
                self,
                f"robot-{i}",
                offsets[i] if i < len(offsets) else 1_000_000_000 * (i + 1) + 12_345 * i,
            )
            for i in range(config.robots)
        ]
        self._robot_by_name = {r.name: r for r in self.robots}
        self.extra_clients: list[SimRobot] = []
        self.core = self._new_core()
        self._worker_generation = 0
        self._worker_busy: Batch | None = None
        self._worker_down_until = 0
        self.worker_busy_us = 0
        self.gateway_restarts = 0
        self._session_robot: dict[str, SimRobot] = {}
        for crash in config.worker_faults.crash_at_us:
            self.loop.at(crash, self._crash_worker)
        for i, robot in enumerate(self.robots):
            # Stagger robot start so ticks do not all coincide.
            self.loop.at(
                1 + (i * binding.control_period_us) // max(1, len(self.robots)), robot.tick
            )
        self.loop.at(100 * MS, self._sweep)

    def _new_core(self) -> GatewayCore:
        binding = self.config.binding
        deployment = Deployment(
            revision_id=self.config.revision_id,
            binding=binding,
            robots=frozenset(r.name for r in self.robots),
        )
        self.group_key = deployment.group_key
        scheduler = Scheduler(
            self.profile, policy=self.config.policy, max_batch=self.config.max_batch
        )
        admission = AdmissionController(
            self.profile,
            max_batch=self.config.max_batch,
            reserve_fraction=self.config.reserve_fraction,
        )
        return GatewayCore(
            deployments=[deployment],
            groups={self.group_key: BatchGroup(self.group_key, scheduler, admission)},
            leases=LeaseTable(self.store, self.config.lease_ttl_us),
        )

    # --- gateway side -------------------------------------------------------------------------

    def gateway_now(self) -> int:
        return self.loop.now_us + self.config.gateway_clock_offset_us

    def gateway_receive(self, robot: SimRobot, item: Outbound, call_id: int) -> None:
        now = self.gateway_now()
        message = item.message
        if item.channel == "open":
            assert isinstance(message, dataplane_pb2.OpenSessionRequest)
            response: Any = self.core.open_session(robot.name, message, now)
            if response.WhichOneof("result") == "accepted":
                self._session_robot[response.accepted.session_id] = robot
            self._respond(robot, call_id, response)
        elif item.channel == "heartbeat":
            assert isinstance(message, dataplane_pb2.HeartbeatRequest)
            self._respond(robot, call_id, self.core.heartbeat(robot.name, message, now))
        elif item.channel == "control":
            assert isinstance(message, dataplane_pb2.ControlRequest)
            self._respond(robot, call_id, self.core.control(robot.name, message, now))
        else:
            assert isinstance(message, dataplane_pb2.RobotMessage)
            deliveries = self.core.receive(robot.name, message, message.ByteSize(), now)
            self._deliver(deliveries)
            self._try_dispatch()

    def _respond(self, robot: SimRobot, call_id: int, response: Any) -> None:
        for at in robot.downlink.deliveries(self.loop.now_us):
            self.loop.at(at, lambda: robot.deliver_response(call_id, response))

    def _deliver(self, deliveries: list[Any]) -> None:
        for delivery in deliveries:
            robot = self._session_robot.get(delivery.session_id)
            if robot is None:
                continue
            for at in robot.downlink.deliveries(self.loop.now_us):
                self.loop.at(at, _deliver_later(robot, delivery.message))

    def _try_dispatch(self) -> None:
        if self._worker_busy is not None or self.loop.now_us < self._worker_down_until:
            return
        batch, dropped = self.core.next_batch(self.group_key, self.gateway_now())
        self._deliver(dropped)
        if batch is None:
            return
        self._worker_busy = batch
        latency = self.worker_model.sample_us(len(batch.requests), self.rng)
        generation = self._worker_generation
        self.worker_busy_us += latency
        self.loop.at(self.loop.now_us + latency, lambda: self._complete(batch, generation))

    def _complete(self, batch: Batch, generation: int) -> None:
        if generation != self._worker_generation or self._worker_busy is not batch:
            return  # the worker crashed or the gateway restarted: never deliver old results
        self._worker_busy = None
        faults = self.config.worker_faults
        outputs: list[NDArray[np.float32]] | PolicyError
        if batch.batch_id in faults.out_of_memory_batches:
            outputs = PolicyError("CUDA out of memory (simulated)", out_of_memory=True)
        else:
            outputs = self.policy.infer([r.inputs for r in batch.requests])
            if batch.batch_id in faults.invalid_output_batches:
                outputs = [np.full_like(o, np.nan) for o in outputs]
            if batch.batch_id in faults.corrupt_output_batches:
                outputs = [np.full_like(o, 1e6) for o in outputs]
        self._deliver(self.core.complete(self.group_key, batch, outputs, self.gateway_now()))
        self._try_dispatch()

    def _crash_worker(self) -> None:
        self._worker_generation += 1
        batch = self._worker_busy
        self._worker_busy = None
        self._worker_down_until = self.loop.now_us + self.config.worker_faults.restart_us
        if batch is not None:
            error = PolicyError("worker process exited (simulated)")
            self._deliver(self.core.complete(self.group_key, batch, error, self.gateway_now()))
        self.loop.at(self._worker_down_until, self._try_dispatch)

    def add_client(self, name: str, *, offset_us: int, start_at_us: int) -> SimRobot:
        """Start another robot client using an existing identity, e.g. a duplicated process."""
        robot = SimRobot(self, name, offset_us)
        self.loop.at(start_at_us, robot.tick)
        self.extra_clients.append(robot)
        return robot

    def restart_gateway(self) -> None:
        """Lose all in-memory gateway state; the lease generation store survives."""
        self.gateway_restarts += 1
        self._worker_generation += 1
        self._worker_busy = None
        self.core = self._new_core()

    def _sweep(self) -> None:
        self.core.sweep(self.gateway_now())
        self._try_dispatch()
        self.loop.at(self.loop.now_us + 100 * MS, self._sweep)

    # --- running ------------------------------------------------------------------------------

    def at(self, time_us: int, action: Callable[[], None]) -> None:
        self.loop.at(time_us, action)

    def run(self, duration_us: int) -> SiteReport:
        self.loop.run_until(self.loop.now_us + duration_us)
        return self.report()

    def report(self) -> SiteReport:
        return SiteReport(
            duration_us=self.loop.now_us,
            robots={
                r.name: RobotReport(
                    state=r.runtime.state,
                    dispatched=r.runtime.dispatched,
                    starvations=r.runtime.counters.get(ReasonCode.BUFFER_STARVED.value, 0),
                    fault=r.runtime.fault.reason if r.runtime.fault else None,
                    counters=dict(r.runtime.counters),
                    state_ticks={s.value: n for s, n in r.states.items()},
                )
                for r in self.robots
            },
            gateway=self.core.status(),
            worker_utilization=self.worker_busy_us / max(self.loop.now_us, 1),
        )


def _deliver_later(robot: SimRobot, message: dataplane_pb2.GatewayMessage) -> Callable[[], None]:
    return lambda: robot.deliver_stream(message)


@dataclass(frozen=True, slots=True)
class RobotReport:
    state: RuntimeState
    dispatched: int
    starvations: int
    fault: ReasonCode | None
    counters: dict[str, int]
    state_ticks: dict[str, int]


@dataclass(frozen=True, slots=True)
class SiteReport:
    duration_us: int
    robots: dict[str, RobotReport]
    gateway: dict[str, Any]
    worker_utilization: float

    def group(self) -> dict[str, Any]:
        groups: dict[str, Any] = self.gateway["groups"]
        return next(iter(groups.values()))
