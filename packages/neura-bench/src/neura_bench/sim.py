"""Discrete-event simulation of a site serving many robots from several batch groups on several
devices, with latency measured on the real device (design §9.5, §10).

The same sans-IO cores as production run here: `RobotRuntime`/`SessionClient` on every robot and
`GatewayCore`/`Scheduler` in the gateway. Only time, the network and the device are simulated:

- a forward pass of batch size b takes a time drawn from the group's measured latency at b;
- the forward passes running on one device share it (processor sharing): with k of them running,
  each advances at 1 / slowdown(k), where slowdown is measured by running k workers concurrently
  on the device. A background (non-policy) job slows every forward pass by its measured factor.

Simulation explores many robots, phases and configurations quickly; the real-time runs check it
against real workers on the device at the configurations they can afford.
"""

from __future__ import annotations

import contextlib
import functools
import heapq
import itertools
import math
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from neura.v1 import dataplane_pb2
from neura_adapters import MockPolicy
from neura_bench.metrics import Result, Snapshot, Window, snapshot, summarize, window
from neura_runtime.binding import ExecutionBinding
from neura_runtime.runtime import RobotRuntime, RuntimeState, TransitionError
from neura_runtime.session import Outbound, SessionClient, SessionConfig
from neura_server.admission import AdmissionController, AdmissionDecision, SessionDemand
from neura_server.gateway import BatchGroup, Delivery, Deployment, GatewayCore
from neura_server.leases import LeaseTable, MemoryGenerationStore
from neura_server.profiles import LatencyProfile, ProfilePoint
from neura_server.scheduler import Batch, Policy, Scheduler
from neura_sim.arm import SimController, SimSensors, SimulatedArm
from neura_sim.network import Link, LinkModel

MS = 1_000
SECOND = 1_000_000
Response = (
    dataplane_pb2.OpenSessionResponse
    | dataplane_pb2.HeartbeatResponse
    | dataplane_pb2.ControlResponse
)


# --- models of the device ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LatencyModel:
    """Measured forward-pass latency per batch size (p50, p99) and peak memory."""

    points: tuple[ProfilePoint, ...]

    def profile(self) -> LatencyProfile:
        return LatencyProfile(list(self.points))

    def memory_mib(self, batch_size: int) -> int:
        return self.profile().memory_mib(batch_size)


def sample_us(measured: LatencyProfile, batch_size: int, rng: random.Random) -> int:
    """A forward-pass time: lognormal around the measured p50 with the measured p99 as its 99th
    percentile. ``measured`` must not be the scheduler's profile, which adapts to observations."""
    p50 = measured.p50_us(batch_size)
    p99 = max(measured.p99_us(batch_size), p50 + 1)
    sigma = math.log(p99 / p50) / 2.326
    return max(1, int(p50 * math.exp(rng.gauss(0.0, sigma))))


@dataclass(frozen=True, slots=True)
class Contention:
    """How forward passes on one device slow each other: ``slowdown[k]`` is the factor each of k
    concurrently running passes takes compared with running alone (1 for k = 1). Larger k
    extrapolate linearly. ``background`` is the factor while a non-policy job runs."""

    slowdown: tuple[tuple[int, float], ...] = ((1, 1.0),)
    background: float = 1.0

    def factor(self, running: int, background: bool) -> float:
        points = sorted(self.slowdown)
        ks = np.array([k for k, _ in points], dtype=np.float64)
        vs = np.array([v for _, v in points], dtype=np.float64)
        if running <= ks[-1] or len(ks) == 1:
            value = float(np.interp(running, ks, vs)) if len(ks) > 1 else float(vs[0] * running)
        else:
            slope = (vs[-1] - vs[-2]) / (ks[-1] - ks[-2])
            value = float(vs[-1] + slope * (running - ks[-1]))
        if len(ks) == 1 and running > 1:
            value = float(running)  # unmeasured: assume plain time-slicing
        return max(value, 1.0) * (self.background if background else 1.0)


class EventLoop:
    def __init__(self) -> None:
        self.now_us = 0
        self._queue: list[tuple[int, int, Callable[[], None]]] = []
        self._order = itertools.count()

    def at(self, time_us: int, action: Callable[[], None]) -> None:
        heapq.heappush(self._queue, (max(int(time_us), self.now_us), next(self._order), action))

    def run_until(self, end_us: int) -> None:
        while self._queue and self._queue[0][0] <= end_us:
            time_us, _, action = heapq.heappop(self._queue)
            self.now_us = time_us
            action()
        self.now_us = max(self.now_us, end_us)


@dataclass(slots=True)
class _Job:
    remaining_us: float
    done: Callable[[], None]


class Device:
    """Processor sharing among the forward passes running on one device."""

    def __init__(self, name: str, contention: Contention, loop: EventLoop) -> None:
        self.name = name
        self._contention = contention
        self._loop = loop
        self._running: dict[int, _Job] = {}
        self._ids = itertools.count()
        self._version = 0
        self._last = 0
        self.background = False
        self.busy_us = 0.0

    def _rate(self) -> float:
        return 1.0 / self._contention.factor(len(self._running), self.background)

    def _advance(self) -> None:
        now = self._loop.now_us
        elapsed = now - self._last
        if elapsed > 0 and self._running:
            rate = self._rate()
            for job in self._running.values():
                job.remaining_us -= elapsed * rate
            self.busy_us += elapsed
        self._last = now

    def _reschedule(self) -> None:
        self._version += 1
        if not self._running:
            return
        remaining = min(job.remaining_us for job in self._running.values())
        version = self._version
        delay = max(1, math.ceil(remaining / self._rate()))
        self._loop.at(self._loop.now_us + delay, lambda: self._fire(version))

    def _fire(self, version: int) -> None:
        if version != self._version:
            return  # the set of running jobs changed since this completion was predicted
        self._advance()
        finished = [j for j, job in self._running.items() if job.remaining_us <= 0.5]
        callbacks = [self._running.pop(j).done for j in finished]
        self._reschedule()
        for callback in callbacks:
            callback()

    def start(self, work_us: float, done: Callable[[], None]) -> None:
        self._advance()
        self._running[next(self._ids)] = _Job(work_us, done)
        self._reschedule()

    def set_background(self, on: bool) -> None:
        self._advance()
        self.background = on
        self._reschedule()


# --- scenario ---------------------------------------------------------------------------------


class AdmitAll(AdmissionController):
    """Benchmarks measure what happens at N robots, so they admit everyone; the admission
    controller's own capacity estimate is reported next to the measurement."""

    def try_admit(self, demand: SessionDemand) -> AdmissionDecision:
        return AdmissionDecision(True, None, "benchmark", 0, 0.0)


@dataclass(frozen=True, slots=True)
class GroupSpec:
    """One batch group: one worker serving one model on one device."""

    name: str
    binding: ExecutionBinding
    device: str
    latency: LatencyModel
    robots: tuple[str, ...]
    policy: Policy = Policy.DEADLINE_FAIRNESS
    max_batch: int = 8


@dataclass(frozen=True, slots=True)
class Scenario:
    groups: tuple[GroupSpec, ...]
    contention: Contention = field(default_factory=Contention)
    # Robot name -> when it starts (true time). Missing robots start at 0.
    starts_us: dict[str, int] = field(default_factory=dict)
    warmup_us: int = 5 * SECOND
    duration_us: int = 60 * SECOND
    # (device, start, end) intervals in true time during which a background job runs.
    background: tuple[tuple[str, int, int], ...] = ()
    link: LinkModel = field(default_factory=LinkModel)
    # A faulted robot is reset by an operator after this long, and keeps going.
    reset_after_us: int = 1 * SECOND
    seed: int = 1


class _Robot:
    def __init__(self, site: Simulation, name: str, binding: ExecutionBinding, revision: str):
        self.site = site
        self.name = name
        self.binding = binding
        arm = SimulatedArm(binding.robot)
        self.arm = arm
        frames = site.frames(binding)
        self.runtime = RobotRuntime(
            binding, SimController(arm), SimSensors(arm, binding.robot, image_bytes=frames)
        )
        self.client = SessionClient(
            self.runtime, SessionConfig(robot_id=name, revision_id=revision)
        )
        self.uplink = Link(site.scenario.link, site.rng)
        self.downlink = Link(site.scenario.link, site.rng)
        self.calls: dict[int, str] = {}
        self.ids = itertools.count(1)
        self.faulted_at: int | None = None
        self.faults = 0

    def tick(self) -> None:
        loop = self.site.loop
        now = loop.now_us
        outbound = self.client.tick(now)
        self.arm.step()
        runtime = self.runtime
        if runtime.state is RuntimeState.READY and runtime.session_id is not None:
            runtime.arm(now)
            outbound.extend(self.client.drain())
        elif runtime.state is RuntimeState.FAULTED:
            if self.faulted_at is None:
                self.faulted_at = now
                self.faults += 1
            elif now - self.faulted_at >= self.site.scenario.reset_after_us:
                with contextlib.suppress(TransitionError):
                    runtime.reset(now, revalidated=True)
                self.faulted_at = None
        for item in outbound:
            self.send(item)
        loop.at(now + self.binding.control_period_us, self.tick)

    def send(self, item: Outbound) -> None:
        loop = self.site.loop
        call_id = next(self.ids)
        message = item.message
        if item.channel != "stream":
            self.calls[call_id] = item.channel
            loop.at(loop.now_us + 500 * MS, lambda: self.timeout(call_id))
        elif isinstance(message, dataplane_pb2.RobotMessage) and message.HasField("observation"):
            env = message.observation.envelope
            self.site.captured[(env.session_id, env.task_epoch, env.sequence)] = loop.now_us
        for at in self.uplink.deliveries(loop.now_us):
            loop.at(at, lambda: self.site.receive(self, item, call_id))

    def timeout(self, call_id: int) -> None:
        channel = self.calls.pop(call_id, None)
        now = self.site.loop.now_us
        if channel == "open":
            self.client.on_open_failed(now)
        elif channel == "heartbeat":
            self.client.on_heartbeat_failed(now)

    def response(self, call_id: int, response: Response) -> None:
        channel = self.calls.pop(call_id, None)
        now = self.site.loop.now_us
        if isinstance(response, dataplane_pb2.OpenSessionResponse) and channel == "open":
            self.client.on_open_response(response, now)
        elif isinstance(response, dataplane_pb2.HeartbeatResponse) and channel == "heartbeat":
            self.client.on_heartbeat_response(response, now)
        elif isinstance(response, dataplane_pb2.ControlResponse) and channel == "control":
            self.client.on_control_response(response, now)

    def stream(self, message: dataplane_pb2.GatewayMessage) -> None:
        self.client.on_gateway_message(message, self.site.loop.now_us)


class Simulation:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.loop = EventLoop()
        self.rng = random.Random(scenario.seed)  # noqa: S311 - modeling, not security
        self._frames: dict[str, dict[str, bytes]] = {}
        self.captured: dict[tuple[str, int, int], int] = {}
        self.devices = {
            g.device: Device(g.device, scenario.contention, self.loop) for g in scenario.groups
        }
        deployments = []
        groups: dict[str, BatchGroup] = {}
        self._group_of: dict[str, GroupSpec] = {}
        self._samplers: dict[str, LatencyProfile] = {}
        self._policies: dict[str, MockPolicy] = {}
        for spec in scenario.groups:
            deployment = Deployment(
                f"rev_{spec.name}", spec.binding, frozenset(spec.robots), spec.device
            )
            deployments.append(deployment)
            key = deployment.group_key
            if key in groups:
                raise ValueError(f"groups {spec.name} and another share the batch group {key}")
            profile = spec.latency.profile()
            scheduler = Scheduler(profile, policy=spec.policy, max_batch=spec.max_batch)
            scheduler.on_dispatch = self._on_dispatch
            scheduler.on_complete = self._on_complete
            groups[key] = BatchGroup(key, scheduler, AdmitAll(profile, max_batch=spec.max_batch))
            self._group_of[key] = spec
            self._samplers[key] = spec.latency.profile()
            self._policies[key] = MockPolicy(spec.binding)
        self.core = GatewayCore(
            deployments=deployments,
            groups=groups,
            leases=LeaseTable(MemoryGenerationStore(), 2 * SECOND),
        )
        self._busy: dict[str, bool] = dict.fromkeys(groups, False)
        self._robots: dict[str, _Robot] = {}
        self._by_session: dict[str, _Robot] = {}
        for spec in scenario.groups:
            for name in spec.robots:
                robot = _Robot(self, name, spec.binding, f"rev_{spec.name}")
                self._robots[name] = robot
                self.loop.at(1 + scenario.starts_us.get(name, 0), robot.tick)
        for device, start, end in scenario.background:
            target = self.devices[device]
            self.loop.at(start, functools.partial(target.set_background, True))
            self.loop.at(end, functools.partial(target.set_background, False))
        self.loop.at(100 * MS, self._sweep)
        self._measuring = False
        self._window_ages: list[int] = []
        self._served_late = 0

    def frames(self, binding: ExecutionBinding) -> dict[str, bytes]:
        key = binding.robot_profile_digest
        if key not in self._frames:
            self._frames[key] = {
                c.role: bytes(c.width * c.height * 3) for c in binding.robot.spec.cameras
            }
        return self._frames[key]

    # --- gateway ----------------------------------------------------------------------------

    def receive(self, robot: _Robot, item: Outbound, call_id: int) -> None:
        now = self.loop.now_us
        message = item.message
        response: Response
        if item.channel == "open":
            assert isinstance(message, dataplane_pb2.OpenSessionRequest)
            response = self.core.open_session(robot.name, message, now)
            if response.WhichOneof("result") == "accepted":
                self._by_session[response.accepted.session_id] = robot
        elif item.channel == "heartbeat":
            assert isinstance(message, dataplane_pb2.HeartbeatRequest)
            response = self.core.heartbeat(robot.name, message, now)
        elif item.channel == "control":
            assert isinstance(message, dataplane_pb2.ControlRequest)
            response = self.core.control(robot.name, message, now)
        else:
            assert isinstance(message, dataplane_pb2.RobotMessage)
            self._deliver(self.core.receive(robot.name, message, message.ByteSize(), now))
            session = self.core.sessions.get(message.observation.envelope.session_id)
            if session is not None:
                self._dispatch(session.group.key)
            return
        for at in robot.downlink.deliveries(now):
            self.loop.at(at, functools.partial(robot.response, call_id, response))

    def _deliver(self, deliveries: list[Delivery]) -> None:
        for delivery in deliveries:
            robot = self._by_session.get(delivery.session_id)
            if robot is None:
                continue
            for at in robot.downlink.deliveries(self.loop.now_us):
                self.loop.at(at, functools.partial(robot.stream, delivery.message))

    def _dispatch(self, key: str) -> None:
        if self._busy[key]:
            return
        batch, dropped = self.core.next_batch(key, self.loop.now_us)
        self._deliver(dropped)
        if batch is None:
            return
        self._busy[key] = True
        spec = self._group_of[key]
        work = sample_us(self._samplers[key], len(batch.requests), self.rng)
        self.devices[spec.device].start(work, lambda: self._complete(key, batch))

    def _complete(self, key: str, batch: Batch) -> None:
        self._busy[key] = False
        outputs = self._policies[key].infer([r.inputs for r in batch.requests])
        self._deliver(self.core.complete(key, batch, outputs, self.loop.now_us))
        self._dispatch(key)

    def _sweep(self) -> None:
        self.core.sweep(self.loop.now_us)
        for key in self._busy:
            self._dispatch(key)
        self.loop.at(self.loop.now_us + 100 * MS, self._sweep)

    def _on_dispatch(self, batch: Batch) -> None:
        if not self._measuring:
            return
        now = self.loop.now_us
        for request in batch.requests:
            env = request.envelope
            captured = self.captured.pop((env.session_id, env.task_epoch, env.sequence), None)
            if captured is not None:
                self._window_ages.append(now - captured)

    def _on_complete(self, batch: Batch, now_us: int) -> None:
        if not self._measuring:
            return
        for request in batch.requests:
            if request.deadline_us > request.received_at_us and now_us > request.deadline_us:
                self._served_late += 1

    # --- running ----------------------------------------------------------------------------

    def run(self) -> Result:
        scenario = self.scenario
        self.loop.run_until(scenario.warmup_us)
        before = self._snapshot()
        self._measuring = True
        self.loop.run_until(scenario.warmup_us + scenario.duration_us)
        after = self._snapshot()
        return summarize(self._window(before, after))

    def _snapshot(self) -> Snapshot:
        return snapshot(
            (g.scheduler.stats for g in self.core.groups.values()),
            {name: (r.runtime, r.faults) for name, r in self._robots.items()},
            {name: d.busy_us / SECOND for name, d in self.devices.items()},
        )

    def _window(self, before: Snapshot, after: Snapshot) -> Window:
        memory: Counter[str] = Counter()
        for key, spec in self._group_of.items():
            sizes = self.core.groups[key].scheduler.stats.batch_sizes
            memory[spec.device] += spec.latency.memory_mib(max(sizes) if sizes else 1)
        return window(
            before,
            after,
            duration_s=self.scenario.duration_us / SECOND,
            devices=len(self.devices),
            served_late=self._served_late,
            ages_at_dispatch_us=self._window_ages,
            peak_memory_mib=max(memory.values()) if memory else 0,
        )


def simulate(scenario: Scenario) -> Result:
    return Simulation(scenario).run()
