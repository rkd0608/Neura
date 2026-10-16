"""The benchmark metrics of design §10, computed the same way for simulated and real-time runs.

Over a measurement window (after warmup):

- deadline misses per 1,000 requests: requests the robot did not get a usable chunk for in time:
  served after their predicted starvation time or rejected by the robot (too old to accept),
  expired before dispatch (too old to dispatch), or failed in a worker. Superseded and fenced
  requests are the robot's own choice, not misses.
- starvation events per robot-hour: a running robot's action queue ran dry (BUFFER_STARVED).
- robot faults per robot-hour: starvation lasted past its grace period and the robot latched a
  fault (an operator reset it).
- late-trimmed actions: actions of accepted chunks that were already in the past on arrival.
- observation age at dispatch: from the robot's capture to the start of the forward pass.
- GPU-hours per robot-hour: devices provisioned per robot served.
- peak memory: the workers' measured peak memory at the largest batch they ran.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from neura_contracts import ReasonCode
from neura_runtime.runtime import RobotRuntime
from neura_server.scheduler import SchedulerStats

MISSED_DROPS = frozenset({ReasonCode.REQUEST_EXPIRED.value})


@dataclass(slots=True)
class Window:
    """Raw counts gathered over a measurement window."""

    duration_s: float
    robots: int
    devices: int
    requests: int = 0  # accepted by the scheduler
    served_late: int = 0
    expired: int = 0
    failed: int = 0
    starved_on_arrival: int = 0
    dispatched: int = 0
    batches: int = 0
    batch_sizes: Counter[int] = field(default_factory=Counter)
    starvations: int = 0
    faults: int = 0
    actions_dispatched: int = 0
    actions_trimmed: int = 0
    chunks_accepted: int = 0
    chunks_rejected_at_robot: int = 0
    ages_at_dispatch_us: list[int] = field(default_factory=list)
    device_busy_s: dict[str, float] = field(default_factory=dict)
    peak_memory_mib: int = 0


@dataclass(frozen=True, slots=True)
class Result:
    robots: int
    devices: int
    duration_s: float
    requests: int
    deadline_misses: int
    misses_per_1000: float
    starvations_per_robot_hour: float
    faults_per_robot_hour: float
    late_trimmed_actions: int
    late_trimmed_per_1000_actions: float
    chunks_rejected_at_robot: int
    age_at_dispatch_p50_ms: float | None
    age_at_dispatch_p99_ms: float | None
    age_at_dispatch_max_ms: float | None
    gpu_hours_per_robot_hour: float
    utilization: dict[str, float]
    mean_batch: float
    batch_sizes: dict[str, int]
    peak_memory_mib: int

    def meets(self, slo: Slo) -> bool:
        return (
            self.misses_per_1000 <= slo.max_misses_per_1000
            and self.starvations_per_robot_hour <= slo.max_starvations_per_robot_hour
            and self.faults_per_robot_hour == 0
            and (slo.max_memory_mib is None or self.peak_memory_mib <= slo.max_memory_mib)
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Slo:
    """What "serves N robots" means in a report: per-robot service no worse than this."""

    max_misses_per_1000: float = 1.0
    max_starvations_per_robot_hour: float = 0.0
    # The device's memory: a configuration that needs more does not serve N robots (None: not
    # checked, as on a CPU host).
    max_memory_mib: int | None = None


def summarize(window: Window) -> Result:
    robot_hours = window.robots * window.duration_s / 3600
    # A chunk the robot rejected (too old, unexpected, corrupt) did not serve it either. A request
    # served after its starvation time is almost always also too old to accept, so the two are
    # not added: the larger count is a lower bound that never counts one request twice.
    misses = (
        window.expired + window.failed + max(window.served_late, window.chunks_rejected_at_robot)
    )
    ages = np.array(window.ages_at_dispatch_us, dtype=np.float64) / 1000

    def pct(q: float) -> float | None:
        return round(float(np.percentile(ages, q)), 3) if ages.size else None

    per_hour = 1 / robot_hours if robot_hours > 0 else 0.0
    return Result(
        robots=window.robots,
        devices=window.devices,
        duration_s=round(window.duration_s, 3),
        requests=window.requests,
        deadline_misses=misses,
        misses_per_1000=round(1000 * misses / window.requests, 3) if window.requests else 0.0,
        starvations_per_robot_hour=round(window.starvations * per_hour, 3),
        faults_per_robot_hour=round(window.faults * per_hour, 3),
        late_trimmed_actions=window.actions_trimmed,
        late_trimmed_per_1000_actions=round(
            1000 * window.actions_trimmed / max(window.actions_dispatched, 1), 3
        ),
        chunks_rejected_at_robot=window.chunks_rejected_at_robot,
        age_at_dispatch_p50_ms=pct(50),
        age_at_dispatch_p99_ms=pct(99),
        age_at_dispatch_max_ms=round(float(ages.max()), 3) if ages.size else None,
        gpu_hours_per_robot_hour=round(window.devices / window.robots, 4) if window.robots else 0,
        utilization={
            d: round(busy / window.duration_s, 4)
            for d, busy in sorted(window.device_busy_s.items())
        },
        mean_batch=round(window.dispatched / window.batches, 3) if window.batches else 0.0,
        batch_sizes={str(k): v for k, v in sorted(window.batch_sizes.items())},
        peak_memory_mib=window.peak_memory_mib,
    )


ROBOT_REJECTIONS = frozenset(
    {
        ReasonCode.CHUNK_STALE_OBSERVATION.value,
        ReasonCode.CHUNK_INTEGRITY_FAILED.value,
        ReasonCode.CHUNK_UNEXPECTED.value,
    }
)


@dataclass(frozen=True, slots=True)
class RobotSnapshot:
    counters: Counter[str]
    dispatched: int
    faults: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Cumulative counts at one moment; a window is the difference of two."""

    submitted: int
    dispatched: int
    batches: int
    batch_sizes: Counter[int]
    expired: int
    failed: int
    starved_on_arrival: int
    robots: dict[str, RobotSnapshot]
    busy_s: dict[str, float]


def snapshot(
    stats: Iterable[SchedulerStats],
    robots: Mapping[str, tuple[RobotRuntime, int]],
    busy_s: Mapping[str, float],
) -> Snapshot:
    """``robots`` maps each robot to its runtime and how many faults it latched so far."""
    stats = list(stats)
    return Snapshot(
        submitted=sum(s.submitted for s in stats),
        dispatched=sum(s.dispatched_requests for s in stats),
        batches=sum(s.batches for s in stats),
        batch_sizes=sum((Counter(s.batch_sizes) for s in stats), Counter()),
        expired=sum(s.dropped.get(ReasonCode.REQUEST_EXPIRED.value, 0) for s in stats),
        failed=sum(s.failed for s in stats),
        starved_on_arrival=sum(s.starved_on_arrival for s in stats),
        robots={
            name: RobotSnapshot(Counter(runtime.counters), runtime.dispatched, faults)
            for name, (runtime, faults) in robots.items()
        },
        busy_s=dict(busy_s),
    )


def window(
    before: Snapshot,
    after: Snapshot,
    *,
    duration_s: float,
    devices: int,
    served_late: int,
    ages_at_dispatch_us: list[int],
    peak_memory_mib: int,
) -> Window:
    result = Window(
        duration_s=duration_s,
        robots=len(after.robots),
        devices=devices,
        requests=after.submitted - before.submitted,
        served_late=served_late,
        expired=after.expired - before.expired,
        failed=after.failed - before.failed,
        starved_on_arrival=after.starved_on_arrival - before.starved_on_arrival,
        dispatched=after.dispatched - before.dispatched,
        batches=after.batches - before.batches,
        batch_sizes=after.batch_sizes - before.batch_sizes,
        ages_at_dispatch_us=ages_at_dispatch_us,
        peak_memory_mib=peak_memory_mib,
    )
    for name, robot in after.robots.items():
        start = before.robots.get(name, RobotSnapshot(Counter(), 0, 0))
        diff = robot.counters - start.counters
        result.starvations += diff.get(ReasonCode.BUFFER_STARVED.value, 0)
        result.faults += robot.faults - start.faults
        result.actions_dispatched += robot.dispatched - start.dispatched
        result.actions_trimmed += diff.get("actions_trimmed", 0)
        result.chunks_accepted += diff.get("chunks_accepted", 0)
        result.chunks_rejected_at_robot += sum(diff.get(code, 0) for code in ROBOT_REJECTIONS)
    result.device_busy_s = {d: busy - before.busy_s.get(d, 0.0) for d, busy in after.busy_s.items()}
    return result
