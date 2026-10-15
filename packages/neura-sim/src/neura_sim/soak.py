"""A long soak of a simulated site with randomly injected faults: ``neura-sim-soak``.

The site simulator runs the production sans-IO cores (robot runtime, session client, gateway,
scheduler, admission, leases) in virtual time, so 72 hours of operation take about two hours on
one CPU core. A seeded fault campaign injects every fault the simulator can model, from the design
§7 fault suite, at random times and on random robots, while an operator stand-in does what people
do on a floor: finishes tasks, takes over, and resets latched faults after a while. The
independent ``InvariantMonitor`` watches everything; the soak also tracks resource use and, after
a fault-free cooldown, requires every robot to be running again.

A run is reproducible from its seed. Exit status 1 means a violation or a robot that did not
recover; the JSON report names each one with its simulated time.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import resource
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from neura_runtime.binding import ExecutionBinding
from neura_runtime.runtime import RuntimeState, TransitionError
from neura_sim.network import Window
from neura_sim.site import MS, SECOND, SimRobot, SimSite, SiteConfig

HOUR = 3600 * SECOND


@dataclass(slots=True)
class SoakConfig:
    hours: float = 72.0
    robots: int = 4
    seed: int = 1
    # Mean time between injected faults (exponentially distributed), simulated seconds.
    mean_fault_interval_s: float = 60.0
    # An operator resets a latched fault after a uniformly random delay in this range.
    recovery_s: tuple[float, float] = (2.0, 30.0)
    # Tasks end at a task boundary after a uniformly random duration in this range.
    task_s: tuple[float, float] = (20.0, 600.0)
    # No faults in the last stretch; every robot must be running when the soak ends.
    cooldown_s: float = 120.0
    # Only these faults (by name); empty means all of them.
    faults: tuple[str, ...] = ()
    progress: Callable[[str], None] | None = None


@dataclass(slots=True)
class _Operator:
    """What happens on the floor around one robot: tasks, takeovers and resets."""

    robot: SimRobot
    task_ends_at: int | None = None
    paused_until: int = 0
    reset_at: int | None = None


@dataclass(slots=True)
class _Hour:
    faults: Counter[str] = field(default_factory=Counter)


class Soak:
    def __init__(self, binding: ExecutionBinding, config: SoakConfig) -> None:
        self.config = config
        self.rng = random.Random(config.seed * 7919 + 17)
        self.site = SimSite(
            SiteConfig(
                binding=binding,
                robots=config.robots,
                seed=config.seed,
                record_dispatches=False,
                # Unrelated clocks on every machine; monotonic clocks never read negative.
                robot_clock_offsets_us=[self.rng.randrange(10**15) for _ in range(config.robots)],
                gateway_clock_offset_us=self.rng.randrange(-(10**15), 10**15),
            )
        )
        if binding.language_input is not None:
            for robot in self.site.robots:
                robot.runtime.set_instruction("pick up the cube and place it in the bin")
        self.operators = [_Operator(robot) for robot in self.site.robots]
        self.injected: Counter[str] = Counter()
        self.recoveries = 0
        self.tasks_completed = 0
        self.findings: list[dict[str, Any]] = []
        self.hours: list[dict[str, Any]] = []
        self._hour = _Hour()
        self._dispatched_at_hour = {r.name: 0 for r in self.site.robots}
        self._faults = {name: weight for name, weight in FAULTS.items() if self._enabled(name)}
        self._fault_names = list(self._faults)
        self._fault_weights = [self._faults[n][0] for n in self._fault_names]
        self._end_us = int(config.hours * HOUR)
        self._faults_until = self._end_us - int(config.cooldown_s * SECOND)
        self._rss_first_hour: int | None = None

    def _enabled(self, name: str) -> bool:
        return not self.config.faults or name in self.config.faults

    # --- running --------------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        site = self.site
        self._schedule_next_fault()
        site.at(500 * MS, self._operate)
        site.at(HOUR, self._hourly)
        site.loop.run_until(self._end_us)
        if self._end_us % HOUR:
            self._close_hour()
        return self._report(time.monotonic() - started)

    def _schedule_next_fault(self) -> None:
        if not self._fault_names:
            return
        gap = self.rng.expovariate(1.0 / self.config.mean_fault_interval_s)
        at = self.site.loop.now_us + max(int(gap * SECOND), 1)
        if at < self._faults_until:
            self.site.at(at, self._inject)

    def _inject(self) -> None:
        name = self.rng.choices(self._fault_names, self._fault_weights)[0]
        self._faults[name][1](self)
        self.injected[name] += 1
        self._hour.faults[name] += 1
        self._schedule_next_fault()

    def _operate(self) -> None:
        site = self.site
        now = site.loop.now_us
        for op in self.operators:
            robot = op.robot
            runtime = robot.runtime
            state = runtime.state
            if state is RuntimeState.FAULTED:
                op.task_ends_at = None
                if op.reset_at is None:
                    op.reset_at = now + self._uniform_us(self.config.recovery_s)
                elif now >= op.reset_at:
                    op.reset_at = None
                    with contextlib.suppress(TransitionError):
                        robot.reset()
                        self.recoveries += 1
                continue
            op.reset_at = None
            robot.auto_arm = now >= op.paused_until
            if state is RuntimeState.RUNNING:
                if op.task_ends_at is None:
                    op.task_ends_at = now + self._uniform_us(self.config.task_s)
                elif now >= op.task_ends_at:
                    op.task_ends_at = None
                    with contextlib.suppress(TransitionError):
                        runtime.complete_task(robot.now())
                        self.tasks_completed += 1
        for robot in site.robots:
            robot.uplink.prune(robot.site.loop.now_us)
            robot.downlink.prune(robot.site.loop.now_us)
        site.at(now + 500 * MS, self._operate)

    def _hourly(self) -> None:
        self._close_hour()
        if self.site.loop.now_us + HOUR <= self._end_us:
            self.site.at(self.site.loop.now_us + HOUR, self._hourly)

    def _close_hour(self) -> None:
        site = self.site
        monitor = site.monitor
        assert monitor is not None
        index = len(self.hours) + 1
        hour_us = site.loop.now_us - (index - 1) * HOUR
        periods = hour_us / site.config.binding.control_period_us
        availability = {}
        for robot in site.robots:
            done = robot.runtime.dispatched - self._dispatched_at_hour[robot.name]
            self._dispatched_at_hour[robot.name] = robot.runtime.dispatched
            availability[robot.name] = round(done / periods, 4) if periods else 0.0
        rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
        if self._rss_first_hour is None:
            self._rss_first_hour = rss_mib
        resources = self._resources()
        self.hours.append(
            {
                "hour": index,
                "availability": availability,
                "faults": dict(sorted(self._hour.faults.items())),
                "violations": monitor.violations,
                "rss_mib": rss_mib,
                "resources": resources,
            }
        )
        self._check_resources(resources, rss_mib)
        self._hour = _Hour()
        if self.config.progress is not None:
            low = min(availability.values()) if availability else 0.0
            self.config.progress(
                f"hour {index}: violations={monitor.violations} "
                f"faults={sum(self.injected.values())} recoveries={self.recoveries} "
                f"min availability={low:.3f} rss={rss_mib} MiB"
            )

    def _resources(self) -> dict[str, int]:
        site = self.site
        groups = list(site.core.groups.values())
        return {
            "gateway_sessions": len(site.core.sessions),
            "leases": len(site.core.leases.active()),
            "admitted_sessions": sum(len(g.admission.admitted) for g in groups),
            "queued_requests": sum(g.scheduler.queued for g in groups),
            "event_queue": len(site.loop),
            "session_routes": len(site._session_robot),
            "clients": len(site.robots) + len(site.extra_clients),
        }

    def _check_resources(self, resources: dict[str, int], rss_mib: int) -> None:
        identities = len(self.site.robots)
        site = self.site
        admitted = {sid for g in site.core.groups.values() for sid in g.admission.admitted}
        problems = []
        if resources["gateway_sessions"] > identities:
            problems.append("more gateway sessions than robot identities")
        if resources["leases"] > identities:
            problems.append("more leases than robot identities")
        if not admitted <= set(site.core.sessions):
            problems.append("admission holds capacity for sessions the gateway has dropped")
        if resources["event_queue"] > 10_000:
            problems.append("the event queue keeps growing")
        if resources["session_routes"] > 50 * identities:
            problems.append("session routes keep growing")
        assert self._rss_first_hour is not None
        if rss_mib > 2 * self._rss_first_hour + 256:
            problems.append(f"memory grew from {self._rss_first_hour} MiB to {rss_mib} MiB")
        for problem in problems:
            self.findings.append(
                {"kind": "resource", "at_us": site.loop.now_us, "detail": problem, **resources}
            )

    def _report(self, wall_s: float) -> dict[str, Any]:
        site = self.site
        monitor = site.monitor
        assert monitor is not None
        not_running = [
            {
                "robot": r.name,
                "state": r.runtime.state.value,
                "fault": r.runtime.fault.reason.value if r.runtime.fault else None,
            }
            for r in site.robots
            if r.runtime.state is not RuntimeState.RUNNING
        ]
        for item in not_running:
            self.findings.append({"kind": "not_recovered", **item})
        counters: Counter[str] = Counter()
        for robot in [*site.robots, *site.extra_clients]:
            counters.update(robot.runtime.counters)
        periods = site.loop.now_us / site.config.binding.control_period_us
        availability = {
            r.name: round(r.runtime.dispatched / periods, 4) if periods else 0.0
            for r in site.robots
        }
        passed = monitor.violations == 0 and not self.findings
        return {
            "passed": passed,
            "simulated_hours": round(site.loop.now_us / HOUR, 3),
            "wall_seconds": round(wall_s, 1),
            "seed": self.config.seed,
            "robots": self.config.robots,
            "model": site.config.binding.artifact.metadata.name,
            "merge": site.config.binding.profile.spec.merge.mode,
            "faults_injected": dict(sorted(self.injected.items())),
            "faults_total": sum(self.injected.values()),
            "recoveries": self.recoveries,
            "tasks_completed": self.tasks_completed,
            "gateway_restarts": site.gateway_restarts,
            "invariants": monitor.summary(),
            "findings": self.findings[:100],
            "availability": availability,
            "runtime_counters": dict(sorted(counters.items())),
            "hours": self.hours,
        }

    # --- helpers --------------------------------------------------------------------------------

    def _uniform_us(self, bounds: tuple[float, float]) -> int:
        return int(self.rng.uniform(*bounds) * SECOND)

    def _robot(self) -> SimRobot:
        return self.rng.choice(self.site.robots)

    def _window(self, low_s: float, high_s: float) -> Window:
        now = self.site.loop.now_us
        return Window(now, now + self._uniform_us((low_s, high_s)))

    def _later(self, window: Window, action: Callable[[], None]) -> None:
        self.site.at(window.end_us, action)


# --- the fault catalog (design §7 fault suite) ---------------------------------------------------


def _delay_spike(soak: Soak) -> None:
    robot = soak._robot()
    link = soak.rng.choice([robot.uplink, robot.downlink])
    extra = int(soak.rng.uniform(0.03, 1.5) * SECOND)
    link.model.spikes.append((soak._window(0.2, 3.0), extra))


def _partition(soak: Soak) -> None:
    robot = soak._robot()
    window = soak._window(0.05, 1.5)
    robot.uplink.model.partitions.append(window)
    robot.downlink.model.partitions.append(window)


def _long_outage(soak: Soak) -> None:
    robot = soak._robot()
    window = soak._window(2.0, 15.0)
    robot.uplink.model.partitions.append(window)
    robot.downlink.model.partitions.append(window)


def _stream_loss(soak: Soak) -> None:
    robot = soak._robot()
    link = soak.rng.choice([robot.uplink, robot.downlink])
    link.model.losses.append((soak._window(1.0, 20.0), soak.rng.uniform(0.05, 0.5)))


def _duplicates_and_reordering(soak: Soak) -> None:
    robot = soak._robot()
    window = soak._window(2.0, 30.0)
    for link in (robot.uplink, robot.downlink):
        link.model.duplicate_probability = 0.3
        link.model.reorder_probability = 0.2
        link.model.reorder_extra_us = 30 * MS

    def restore() -> None:
        for link in (robot.uplink, robot.downlink):
            link.model.duplicate_probability = 0.0
            link.model.reorder_probability = 0.0

    soak._later(window, restore)


def _site_network_degraded(soak: Soak) -> None:
    window = soak._window(5.0, 60.0)
    extra = int(soak.rng.uniform(0.005, 0.08) * SECOND)
    for robot in soak.site.robots:
        robot.uplink.model.spikes.append((window, extra))
        robot.downlink.model.spikes.append((window, extra))


def _worker_crash(soak: Soak) -> None:
    soak.site.crash_worker(soak._uniform_us((0.1, 0.6)))


def _worker_crash_long(soak: Soak) -> None:
    soak.site.crash_worker(soak._uniform_us((1.0, 5.0)))


def _worker_out_of_memory(soak: Soak) -> None:
    soak.site.config.worker_faults.next_batches.extend(["out_of_memory"] * soak.rng.randint(1, 3))


def _worker_invalid_output(soak: Soak) -> None:
    soak.site.config.worker_faults.next_batches.extend(["invalid"] * soak.rng.randint(1, 3))


def _worker_out_of_range_output(soak: Soak) -> None:
    soak.site.config.worker_faults.next_batches.append("corrupt")


def _gateway_restart(soak: Soak) -> None:
    soak.site.restart_gateway()


def _camera_missing(soak: Soak) -> None:
    robot = soak._robot()
    role = soak.rng.choice(sorted(robot.sensors.cameras))
    robot.sensors.missing_cameras.add(role)
    soak._later(soak._window(0.1, 3.0), lambda: robot.sensors.missing_cameras.discard(role))


def _camera_skew(soak: Soak) -> None:
    robot = soak._robot()
    role = soak.rng.choice(sorted(robot.sensors.cameras))
    lag = robot.sensors.camera_lag_us
    lag[role] = int(soak.rng.uniform(0.06, 0.3) * SECOND)
    soak._later(soak._window(0.1, 3.0), lambda: lag.update({role: 0}))


def _sensors_unavailable(soak: Soak) -> None:
    robot = soak._robot()
    robot.sensors.unavailable = True
    soak._later(soak._window(0.05, 2.0), lambda: setattr(robot.sensors, "unavailable", False))


def _operator_takeover(soak: Soak) -> None:
    op = soak.rng.choice(soak.operators)
    op.robot.runtime.operator_takeover(op.robot.now())
    op.robot.auto_arm = False
    op.task_ends_at = None
    op.paused_until = soak.site.loop.now_us + soak._uniform_us((1.0, 20.0))


def _second_client(soak: Soak) -> None:
    robot = soak._robot()
    duplicate = soak.site.add_client(
        robot.name,
        offset_us=soak.rng.randrange(10**15),
        start_at_us=soak.site.loop.now_us,
    )
    if robot.runtime.binding.language_input is not None:
        duplicate.runtime.set_instruction("pick up the cube and place it in the bin")
    soak._later(soak._window(2.0, 30.0), duplicate.stop)


def _controller_failure(soak: Soak) -> None:
    robot = soak._robot()
    robot.controller.fail_commands = True
    soak._later(soak._window(0.05, 1.0), lambda: setattr(robot.controller, "fail_commands", False))


def _actuator_lock_lost(soak: Soak) -> None:
    robot = soak._robot()
    robot.controller.authority = False
    soak._later(soak._window(0.05, 2.0), lambda: setattr(robot.controller, "authority", True))


def _oversized_message(soak: Soak) -> None:
    soak._robot().oversize_next = True


def _credential_revoked(soak: Soak) -> None:
    """An operator revokes a robot's credential, which ends its session; it is re-issued and the
    robot opens a new session under a new lease generation."""
    robot = soak._robot()
    soak.site.core.close_robots_without_credentials(
        {r.name for r in soak.site.robots if r.name != robot.name}
    )


FAULTS: dict[str, tuple[float, Callable[[Soak], None]]] = {
    "delay_spike": (3.0, _delay_spike),
    "partition": (2.0, _partition),
    "long_outage": (0.5, _long_outage),
    "stream_loss": (1.5, _stream_loss),
    "duplicates_and_reordering": (1.5, _duplicates_and_reordering),
    "site_network_degraded": (0.5, _site_network_degraded),
    "worker_crash": (1.5, _worker_crash),
    "worker_crash_long": (0.3, _worker_crash_long),
    "worker_out_of_memory": (1.0, _worker_out_of_memory),
    "worker_invalid_output": (1.0, _worker_invalid_output),
    "worker_out_of_range_output": (0.3, _worker_out_of_range_output),
    "gateway_restart": (0.5, _gateway_restart),
    "camera_missing": (1.0, _camera_missing),
    "camera_skew": (1.0, _camera_skew),
    "sensors_unavailable": (0.5, _sensors_unavailable),
    "operator_takeover": (1.0, _operator_takeover),
    "second_client": (0.7, _second_client),
    "controller_failure": (0.3, _controller_failure),
    "actuator_lock_lost": (0.3, _actuator_lock_lost),
    "oversized_message": (0.5, _oversized_message),
    "credential_revoked": (0.5, _credential_revoked),
}


# --- command line -------------------------------------------------------------------------------


# Merge modes the example models are validated for (design §5.3).
MERGES: dict[str, dict[str, Any]] = {
    "replace": {"mode": "replace"},
    "blend": {"mode": "blend", "window_actions": 5, "new_chunk_weights": [0.2, 0.4, 0.6, 0.8, 1.0]},
}


def example_binding(contracts: Path, model: str, merge: str | None = None) -> ExecutionBinding:
    """The example contracts for ``model`` on the SO-101, optionally with another merge mode."""
    from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, parse_document

    def read(name: str) -> dict[str, Any]:
        data = yaml.safe_load((contracts / name).read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data

    artifact = parse_document(read(f"{model}-so101.artifact.yaml"))
    robot = parse_document(read("so101.robot.yaml"))
    profile_data = read(f"{model}-so101-30hz.profile.yaml")
    if merge is not None:
        profile_data["spec"]["merge"] = MERGES[merge]
    profile = parse_document(profile_data)
    assert isinstance(artifact, ModelArtifact)
    assert isinstance(robot, RobotProfile)
    assert isinstance(profile, ExecutionProfile)
    return ExecutionBinding(artifact, robot, profile)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neura-sim-soak", description=__doc__.split("\n\n")[0])
    parser.add_argument("--hours", type=float, default=72.0, help="simulated hours (default 72)")
    parser.add_argument("--robots", type=int, default=4)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--mean-fault-interval", type=float, default=60.0, help="simulated seconds between faults"
    )
    parser.add_argument("--cooldown", type=float, default=120.0, help="fault-free tail, seconds")
    parser.add_argument("--fault", action="append", default=[], choices=sorted(FAULTS))
    parser.add_argument("--contracts", type=Path, default=Path("examples/contracts"))
    parser.add_argument("--model", default="act", help="example model: act or smolvla")
    parser.add_argument(
        "--merge", choices=sorted(MERGES), help="override the example profile's merge mode"
    )
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    args = parser.parse_args(argv)

    binding = example_binding(args.contracts, args.model, args.merge)
    config = SoakConfig(
        hours=args.hours,
        robots=args.robots,
        seed=args.seed,
        mean_fault_interval_s=args.mean_fault_interval,
        cooldown_s=min(args.cooldown, args.hours * 3600 / 2),
        faults=tuple(args.fault),
        progress=lambda line: print(line, file=sys.stderr, flush=True),
    )
    report = Soak(binding, config).run()
    text = json.dumps(report, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    summary = {
        k: report[k]
        for k in (
            "passed",
            "simulated_hours",
            "wall_seconds",
            "faults_total",
            "recoveries",
            "tasks_completed",
        )
    }
    summary["violations"] = report["invariants"]["violations"]
    summary["findings"] = len(report["findings"])
    print(json.dumps(summary))
    if not report["passed"]:
        print(json.dumps(report["invariants"]["first"] + report["findings"][:10], indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
