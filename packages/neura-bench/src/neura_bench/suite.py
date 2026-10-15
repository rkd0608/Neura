"""The GPU-sharing benchmark suite (design §9.5, §10, §16 step 0).

1. Build the models: synthetic ACT (the reference policy) and Diffusion Policy (a second
   architecture), plus fine-tunes of ACT that differ only in their weights.
2. Measure on the device, through real worker processes: latency per batch size, contention
   between co-located workers, a background job's effect, and multi-adapter batching's cost and
   parity.
3. Simulate every sharing case against the dedicated baselines over a sweep of robot counts,
   using the measured numbers, and find each configuration's capacity at the SLO.
4. Validate: run the main configurations in real time (real gateway, real workers, gRPC robots)
   and compare with the simulation at the same robot count.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from neura_adapters.synthetic import Size
from neura_bench import measure, realtime
from neura_bench.deployments import DeploymentFiles, synthetic, write_files
from neura_bench.metrics import Result, Slo
from neura_bench.sim import SECOND, GroupSpec, LatencyModel, Scenario, simulate
from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, parse_document
from neura_runtime.binding import ExecutionBinding
from neura_server import capacity
from neura_server.config import ServingConfig, profile_batch_sizes
from neura_server.profiles import ProfilePoint
from neura_server.scheduler import Policy
from neura_sim.network import LinkModel

log = logging.getLogger("neura.bench")

# Configurations (design §10). Every case compares Neura's sharing with the dedicated baselines.
DEDICATED = "dedicated_gpu"  # one device per robot, batch size 1
DEDICATED_SHARED = "dedicated_shared_gpu"  # one worker process per robot, all on one device
NEURA_FIFO = "neura_fifo"
NEURA_RR = "neura_round_robin"
NEURA_DEADLINE = "neura_deadline_fairness"
NEURA_MULTI_ADAPTER = "neura_multi_adapter"
NEURA_CO_LOCATED = "neura_co_located"
POLICIES = {
    NEURA_FIFO: Policy.FIFO,
    NEURA_RR: Policy.ROUND_ROBIN,
    NEURA_DEADLINE: Policy.DEADLINE_FAIRNESS,
}

SAME_WEIGHTS = "same_weights"
MULTI_ADAPTER = "multi_adapter"
CO_LOCATED = "co_located_fine_tunes"
DIFFERENT = "different_architectures"
CASES = {
    SAME_WEIGHTS: (DEDICATED, DEDICATED_SHARED, NEURA_FIFO, NEURA_RR, NEURA_DEADLINE),
    MULTI_ADAPTER: (DEDICATED, DEDICATED_SHARED, NEURA_MULTI_ADAPTER),
    CO_LOCATED: (DEDICATED, DEDICATED_SHARED, NEURA_CO_LOCATED),
    DIFFERENT: (DEDICATED, DEDICATED_SHARED, NEURA_CO_LOCATED),
}
PHASED = "phased"  # robots' refill times spread over the request interval
SYNCHRONIZED = "synchronized"  # every robot starts in the same control period: aligned bursts
BACKGROUND = "background"  # phased, with a non-policy job running on the device
WORKLOADS = {
    SAME_WEIGHTS: (PHASED, SYNCHRONIZED, BACKGROUND),
    MULTI_ADAPTER: (PHASED,),
    CO_LOCATED: (PHASED, BACKGROUND),
    DIFFERENT: (PHASED,),
}


@dataclass(frozen=True, slots=True)
class SuiteConfig:
    device: str = "cuda:0"
    size: Size = "base"
    image: tuple[int, int] | None = None  # None: the example's 480 x 640 cameras
    precision: str = "bf16"
    max_batch: int = 16
    measure_seconds: float = 10.0
    contention_workers: int = 4
    fine_tunes: int = 4
    adapter_rank: int = 16
    sim_robots: tuple[int, ...] = (1, 2, 4, 8, 12, 16, 24, 32, 48, 64, 96, 128)
    sim_warmup_s: float = 10.0
    sim_duration_s: float = 120.0
    realtime_robots: tuple[int, ...] = (1, 4, 8, 16)
    realtime_warmup_s: float = 10.0
    realtime_duration_s: float = 60.0
    slo: Slo = field(default_factory=Slo)
    seed: int = 1


PRESETS = {
    "gpu": SuiteConfig(),
    # A development host: a small model at low resolution, short runs. Its numbers describe the
    # host's CPU, never a GPU.
    "cpu": SuiteConfig(
        device="cpu",
        size="small",
        image=(96, 128),
        precision="fp32",
        max_batch=8,
        measure_seconds=3.0,
        contention_workers=3,
        fine_tunes=2,
        sim_robots=(1, 2, 4, 8, 12, 16, 24, 32),
        sim_warmup_s=5.0,
        sim_duration_s=60.0,
        realtime_robots=(1, 4),
        realtime_warmup_s=5.0,
        realtime_duration_s=20.0,
    ),
    # For tests of the harness itself.
    "smoke": SuiteConfig(
        device="cpu",
        size="tiny",
        image=(48, 64),
        precision="fp32",
        max_batch=4,
        measure_seconds=0.5,
        contention_workers=2,
        fine_tunes=2,
        adapter_rank=4,
        sim_robots=(1, 2, 4),
        sim_warmup_s=2.0,
        sim_duration_s=10.0,
        realtime_robots=(2,),
        realtime_warmup_s=2.0,
        realtime_duration_s=4.0,
    ),
}


# --- models ---------------------------------------------------------------------------------


@dataclass(slots=True)
class Models:
    act: DeploymentFiles
    fine_tunes: list[DeploymentFiles]
    diffusion: DeploymentFiles
    directory: Path
    _copies: dict[tuple[str, int], DeploymentFiles] = field(default_factory=dict)

    def copy(self, base: DeploymentFiles, index: int) -> DeploymentFiles:
        """The same weights as another artifact: its own batch group and worker process."""
        key = (str(base.artifact), index)
        if key not in self._copies:
            artifact = json.loads(base.artifact.read_text())
            robot = json.loads(base.robot_profile.read_text())
            profile = json.loads(base.execution_profile.read_text())
            artifact["metadata"]["version"] = f"{artifact['metadata']['version']}-copy-{index}"
            profile["spec"]["binding"]["model_artifact"] = parse_document(artifact).digest()
            directory = self.directory / "copies" / f"{base.artifact.parent.name}-{index}"
            self._copies[key] = write_files(directory, (artifact, robot, profile), base.checkpoint)
        return self._copies[key]


def build_models(config: SuiteConfig, directory: Path) -> Models:
    common: dict[str, Any] = {
        "size": config.size,
        "image": config.image,
        "device": "cpu" if config.device == "cpu" else "cuda",
        "precision": config.precision,
    }
    act = synthetic(directory / "act", model="act", seed=0, **common)
    tunes = [
        synthetic(directory / f"act-tune-{i}", model="act", seed=i, **common)
        for i in range(1, config.fine_tunes + 1)
    ]
    diffusion = synthetic(directory / "diffusion", model="diffusion", seed=0, **common)
    return Models(act, tunes, diffusion, directory)


# --- measurements ---------------------------------------------------------------------------


@dataclass(slots=True)
class Measurements:
    act: measure.LatencyMeasurement
    diffusion: measure.LatencyMeasurement
    contention: measure.ContentionMeasurement
    multi_adapter: measure.MultiAdapterMeasurement

    def multi_adapter_model(self) -> LatencyModel:
        """ACT's measured latency scaled by the measured cost of mixing adapters in a batch.
        An overhead below 1 is timing noise; the model never makes mixing faster."""
        sizes = sorted(self.multi_adapter.base)
        points = []
        for point in self.act.model.points:
            nearest = min(sizes, key=lambda s: abs(s - point.batch_size))
            factor = max(1.0, self.multi_adapter.overhead(nearest))
            points.append(
                ProfilePoint(
                    point.batch_size,
                    int(point.p50_us * factor),
                    int(point.p99_us * factor),
                    point.memory_mib,
                )
            )
        return LatencyModel(tuple(points))

    def to_json(self) -> dict[str, Any]:
        return {
            "latency": {"act": self.act.to_json(), "diffusion": self.diffusion.to_json()},
            "contention": self.contention.to_json(),
            "multi_adapter": self.multi_adapter.to_json(),
        }


async def take_measurements(config: SuiteConfig, models: Models) -> Measurements:
    sizes = profile_batch_sizes(config.max_batch)
    log.info("measuring ACT latency on %s", config.device)
    act = await measure.latency(
        models.act, config.device, batch_sizes=sizes, seconds=config.measure_seconds
    )
    log.info("measuring Diffusion Policy latency")
    diffusion = await measure.latency(
        models.diffusion, config.device, batch_sizes=sizes, seconds=config.measure_seconds
    )
    log.info("measuring contention between %d co-located workers", config.contention_workers)
    workers = [models.copy(models.act, i) for i in range(config.contention_workers)]
    contention = await measure.contention(
        workers,
        config.device,
        seconds=config.measure_seconds,
        background=measure.background_command(config.device),
    )
    log.info("measuring multi-adapter batching")
    multi = await asyncio.to_thread(
        measure.multi_adapter,
        models.act,
        config.device,
        batch_sizes=sizes,
        rank=config.adapter_rank,
        iterations=max(10, int(config.measure_seconds * 10)),
    )
    return Measurements(act, diffusion, contention, multi)


# --- scenarios ------------------------------------------------------------------------------


def _bindings(base: DeploymentFiles, count: int) -> Iterator[ExecutionBinding]:
    """Distinct artifacts (one batch group each) with the base's contracts, for simulation."""
    artifact = json.loads(base.artifact.read_text())
    robot = json.loads(base.robot_profile.read_text())
    profile = json.loads(base.execution_profile.read_text())
    r = parse_document(robot)
    assert isinstance(r, RobotProfile)
    for index in range(count):
        variant = json.loads(json.dumps(artifact))
        variant["metadata"]["version"] = f"{artifact['metadata']['version']}-sim-{index}"
        a = parse_document(variant)
        assert isinstance(a, ModelArtifact)
        bound = json.loads(json.dumps(profile))
        bound["spec"]["binding"]["model_artifact"] = a.digest()
        p = parse_document(bound)
        assert isinstance(p, ExecutionProfile)
        yield ExecutionBinding(a, r, p)


@dataclass(frozen=True, slots=True)
class Plan:
    """What runs in one (case, configuration, workload, N) cell: groups of robots per model."""

    groups: tuple[tuple[str, str, int], ...]  # (group name, model kind, robots)
    devices: str  # "one" (all on one device) or "each" (a device per group)
    policy: Policy
    max_batch: int


def model_kinds(case: str, config: SuiteConfig) -> list[str]:
    """The models a case's robots run, split evenly between them."""
    return {
        SAME_WEIGHTS: ["act"],
        MULTI_ADAPTER: ["act"],
        CO_LOCATED: [f"tune-{i}" for i in range(config.fine_tunes)],
        DIFFERENT: ["act", "diffusion"],
    }[case]


def plan(case: str, configuration: str, robots: int, config: SuiteConfig) -> Plan:
    def split(kinds: list[str]) -> list[tuple[str, int]]:
        shares = [
            robots // len(kinds) + (1 if i < robots % len(kinds) else 0) for i in range(len(kinds))
        ]
        return [(k, n) for k, n in zip(kinds, shares, strict=True) if n > 0]

    kinds = model_kinds(case, config)
    if configuration in (DEDICATED, DEDICATED_SHARED):
        # One robot per group: one worker process per robot.
        per_robot = [(kind, 1) for kind, n in split(kinds) for _ in range(n)]
        groups = tuple((f"r{i}", kind, 1) for i, (kind, _) in enumerate(per_robot))
        devices = "each" if configuration == DEDICATED else "one"
        return Plan(groups, devices, Policy.FIFO, 1)
    if configuration == NEURA_MULTI_ADAPTER:
        return Plan(
            (("multi", "multi_adapter", robots),), "one", Policy.DEADLINE_FAIRNESS, config.max_batch
        )
    if configuration == NEURA_CO_LOCATED:
        groups = tuple((f"g-{kind}", kind, n) for kind, n in split(kinds))
        return Plan(groups, "one", Policy.DEADLINE_FAIRNESS, config.max_batch)
    return Plan((("shared", "act", robots),), "one", POLICIES[configuration], config.max_batch)


def _interval_s(binding: ExecutionBinding) -> float:
    chunk = binding.profile.spec.chunk
    return (
        (chunk.actions_per_chunk - chunk.refill_threshold_actions) * binding.control_period_us / 1e6
    )


def starts(workload: str, groups: list[tuple[list[str], ExecutionBinding]]) -> dict[str, float]:
    """Robot start offsets in seconds: phased spreads each model's robots over its request
    interval; synchronized starts every robot in the same control period."""
    offsets: dict[str, float] = {}
    everyone = [(name, binding) for names, binding in groups for name in names]
    for index, (name, binding) in enumerate(everyone):
        if workload == SYNCHRONIZED:
            offsets[name] = 0.0
        else:
            offsets[name] = _interval_s(binding) * index / len(everyone)
    return offsets


class Builder:
    """Turns plans into simulated and real-time scenarios."""

    def __init__(self, config: SuiteConfig, models: Models, measured: Measurements) -> None:
        self.config = config
        self.models = models
        self.measured = measured
        self._sim_bindings: dict[str, list[ExecutionBinding]] = {}
        # Robot-to-gateway latency in simulation, until calibrated from a real run.
        self.link = LinkModel()

    def latency(self, kind: str) -> LatencyModel:
        if kind == "diffusion":
            return self.measured.diffusion.model
        if kind == "multi_adapter":
            return self.measured.multi_adapter_model()
        return self.measured.act.model

    def files(self, kind: str) -> DeploymentFiles:
        if kind == "diffusion":
            return self.models.diffusion
        if kind.startswith("tune-"):
            return self.models.fine_tunes[int(kind.split("-")[1])]
        return self.models.act

    def _sim_binding(self, kind: str, index: int) -> ExecutionBinding:
        pool = self._sim_bindings.setdefault(kind, [])
        while len(pool) <= index:
            pool.extend(_bindings(self.files(kind), 16))
        return pool[index]

    def simulation(
        self,
        p: Plan,
        workload: str,
        robots: int,
        *,
        warmup_s: float | None = None,
        duration_s: float | None = None,
    ) -> Scenario:
        groups = []
        named: list[tuple[list[str], ExecutionBinding]] = []
        used: dict[str, int] = {}
        counter = 0
        for index, (name, kind, n) in enumerate(p.groups):
            binding = self._sim_binding(kind, used.get(kind, 0))
            used[kind] = used.get(kind, 0) + 1
            names = [f"robot-{counter + i}" for i in range(n)]
            counter += n
            named.append((names, binding))
            device = f"dev-{index}" if p.devices == "each" else "dev-0"
            groups.append(
                GroupSpec(
                    name, binding, device, self.latency(kind), tuple(names), p.policy, p.max_batch
                )
            )
        offsets = starts(workload, named)
        warmup = self.config.sim_warmup_s if warmup_s is None else warmup_s
        duration = self.config.sim_duration_s if duration_s is None else duration_s
        end = int((warmup + duration + 10) * SECOND)
        devices = sorted({g.device for g in groups})
        return Scenario(
            groups=tuple(groups),
            contention=self.measured.contention.contention,
            starts_us={n: int(s * SECOND) for n, s in offsets.items()},
            warmup_us=int(warmup * SECOND),
            duration_us=int(duration * SECOND),
            link=self.link,
            background=tuple((d, 0, end) for d in devices) if workload == BACKGROUND else (),
            seed=self.config.seed + robots,
        )

    def realtime(self, p: Plan, workload: str) -> realtime.RealScenario:
        groups = []
        named: list[tuple[list[str], ExecutionBinding]] = []
        used: dict[str, int] = {}
        counter = 0
        for name, kind, n in p.groups:
            base = self.files(kind)
            copy_index = used.get(kind, 0)
            used[kind] = copy_index + 1
            # Groups of the same model need their own artifact to get their own worker.
            files = base if copy_index == 0 else self.models.copy(base, copy_index)
            names = [f"robot-{counter + i}" for i in range(n)]
            counter += n
            named.append((names, files.binding))
            groups.append(realtime.RealGroup(name, files, tuple(names), self.latency(kind)))
        return realtime.RealScenario(
            groups=tuple(groups),
            device=self.config.device,
            policy=p.policy,
            max_batch=p.max_batch,
            starts_s=starts(workload, named),
            warmup_s=self.config.realtime_warmup_s,
            duration_s=self.config.realtime_duration_s,
            background=workload == BACKGROUND,
        )


# --- running --------------------------------------------------------------------------------


@dataclass(slots=True)
class Cell:
    case: str
    workload: str
    configuration: str
    robots: int
    result: Result


def sweep(builder: Builder, progress: Callable[[str], None] = log.info) -> list[Cell]:
    """Every case, workload and configuration over the robot counts, in simulation. A
    configuration stops after it misses the SLO at two robot counts in a row. The dedicated
    one-device-per-robot baseline is independent of N and runs once."""
    config = builder.config
    cells = []
    for case, configurations in CASES.items():
        for workload in WORKLOADS[case]:
            for configuration in configurations:
                failures = 0
                counts = config.sim_robots
                if configuration == DEDICATED:
                    counts = (len(model_kinds(case, config)),)  # one robot of each model
                for robots in counts:
                    p = plan(case, configuration, robots, config)
                    result = simulate(builder.simulation(p, workload, robots))
                    cells.append(Cell(case, workload, configuration, robots, result))
                    progress(
                        f"sim {case}/{workload}/{configuration} N={robots}: "
                        f"{result.misses_per_1000} misses/1000, "
                        f"{result.starvations_per_robot_hour} starvations/robot-h"
                    )
                    failures = 0 if result.meets(config.slo) else failures + 1
                    if failures >= 2:
                        break
    return cells


@dataclass(slots=True)
class Validation:
    case: str
    workload: str
    configuration: str
    robots: int
    real: realtime.RealResult
    simulated: Result


@dataclass(frozen=True, slots=True)
class Calibration:
    """The simulator's robot-to-gateway latency, set from one robot alone in real time: the
    observation age at dispatch then is the link plus the gateway's intake (decode, checks)."""

    age_p50_ms: float
    age_p99_ms: float
    link: LinkModel

    def to_json(self) -> dict[str, Any]:
        return {
            "real_age_at_dispatch_p50_ms": self.age_p50_ms,
            "real_age_at_dispatch_p99_ms": self.age_p99_ms,
            "link_base_ms": self.link.base_us / 1000,
            "link_jitter_ms": self.link.jitter_us / 1000,
        }


async def calibrate(builder: Builder) -> Calibration:
    p = replace(plan(SAME_WEIGHTS, DEDICATED, 1, builder.config), devices="one")
    run = await realtime.run(builder.realtime(p, PHASED))
    p50 = run.result.age_at_dispatch_p50_ms or 0.5
    p99 = run.result.age_at_dispatch_p99_ms or p50
    # Uniform jitter on [0, j] puts the 99th percentile at about base + j.
    link = LinkModel(
        base_us=max(1, int(p50 * 1000 * 0.9)), jitter_us=max(0, int((p99 - p50 * 0.9) * 1000))
    )
    builder.link = link
    log.info(
        "calibrated the simulated link: %s ms + up to %s ms",
        link.base_us / 1000,
        link.jitter_us / 1000,
    )
    return Calibration(p50, p99, link)


def validation_plan(config: SuiteConfig) -> list[tuple[str, str, str, int]]:
    largest = max(config.realtime_robots)
    runs = [(SAME_WEIGHTS, PHASED, DEDICATED, 1)]
    runs += [(SAME_WEIGHTS, PHASED, NEURA_DEADLINE, n) for n in config.realtime_robots]
    runs += [
        (SAME_WEIGHTS, PHASED, NEURA_FIFO, largest),
        (SAME_WEIGHTS, PHASED, DEDICATED_SHARED, min(largest, config.contention_workers)),
        (SAME_WEIGHTS, SYNCHRONIZED, NEURA_DEADLINE, largest),
        (SAME_WEIGHTS, BACKGROUND, NEURA_DEADLINE, largest),
        (CO_LOCATED, PHASED, NEURA_CO_LOCATED, max(largest, config.fine_tunes)),
        (DIFFERENT, PHASED, NEURA_CO_LOCATED, max(largest, 2)),
    ]
    return runs


async def validate(
    builder: Builder, progress: Callable[[str], None] = log.info
) -> list[Validation]:
    """Real-time runs of the main configurations, each next to its simulation."""
    out = []
    for case, workload, configuration, robots in validation_plan(builder.config):
        p = plan(case, configuration, robots, builder.config)
        if configuration == DEDICATED:
            p = replace(p, devices="one")  # one robot alone on the device is the baseline
        real = await realtime.run(builder.realtime(p, workload))
        simulated = simulate(
            builder.simulation(
                p,
                workload,
                robots,
                warmup_s=builder.config.realtime_warmup_s,
                duration_s=builder.config.realtime_duration_s,
            )
        )
        out.append(Validation(case, workload, configuration, robots, real, simulated))
        progress(
            f"real {case}/{workload}/{configuration} N={robots}: "
            f"{real.result.misses_per_1000} misses/1000 (simulated {simulated.misses_per_1000}), "
            f"age p99 {real.result.age_at_dispatch_p99_ms} ms "
            f"(simulated {simulated.age_at_dispatch_p99_ms})"
        )
    return out


def capacity_at_slo(cells: list[Cell], slo: Slo) -> dict[str, dict[str, dict[str, Any]]]:
    """Per case and workload, each configuration's robots per device at the SLO: the largest
    N such that it and every smaller N measured meet it. Dedicated: 1 robot per device if one
    robot alone meets it."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for cell in cells:
        entry = (
            out.setdefault(cell.case, {})
            .setdefault(cell.workload, {})
            .setdefault(
                cell.configuration, {"robots_per_device": 0, "ok": True, "first_failure": None}
            )
        )
        if not entry["ok"]:
            continue
        if cell.result.meets(slo):
            if cell.configuration == DEDICATED:
                entry["robots_per_device"] = 1
            else:
                entry["robots_per_device"] = cell.robots
        else:
            entry["ok"] = False
            entry["first_failure"] = cell.robots
    for workloads in out.values():
        for configurations in workloads.values():
            for entry in configurations.values():
                n = entry.pop("robots_per_device")
                entry.pop("ok")
                entry["robots_per_device"] = n
                entry["gpu_hours_per_robot_hour"] = round(1 / n, 4) if n else None
    return out


def admission_estimate(
    config: SuiteConfig, models: Models, measured: Measurements
) -> dict[str, Any]:
    """What the gateway's admission control would admit for ACT on this device: it must not
    promise more than the measured capacity."""
    serving = ServingConfig(max_batch=config.max_batch)
    return capacity.estimate(
        models.act.binding,
        serving,
        rtt_p99_ms=2.0,
        measured=list(measured.act.model.points),
        device=config.device,
    )


def clean(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
