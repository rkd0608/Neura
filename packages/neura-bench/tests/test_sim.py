"""The benchmark simulator's device model and metrics, and the suite's planning (no torch)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from neura_bench import realtime, suite
from neura_bench.deployments import VlaTiming, contracts, write_files
from neura_bench.metrics import Result, Slo
from neura_bench.sim import (
    MS,
    SECOND,
    Contention,
    Device,
    EventLoop,
    GroupSpec,
    LatencyModel,
    Scenario,
    simulate,
)
from neura_server.profiles import ProfilePoint
from neura_server.scheduler import Policy

FAST = LatencyModel(
    (ProfilePoint(1, 20 * MS, 25 * MS, 500), ProfilePoint(8, 35 * MS, 45 * MS, 900))
)
SLOW = LatencyModel((ProfilePoint(1, 400 * MS, 450 * MS, 500),))


def test_processor_sharing_slows_concurrent_jobs_by_the_measured_factor() -> None:
    loop = EventLoop()
    device = Device("d", Contention(((1, 1.0), (2, 1.5))), loop)
    done: dict[str, int] = {}

    def finished(name: str) -> Callable[[], None]:
        return lambda: done.update({name: loop.now_us})

    device.start(100, finished("a"))
    loop.at(50, lambda: device.start(100, finished("b")))
    loop.run_until(10_000)
    # a: 50 alone, then its remaining 50 at rate 1/1.5 -> 75 more: done at 125.
    # b: 75 at rate 1/1.5 (50 of work) until a ends, then 50 alone: done at 175.
    assert done == {"a": 125, "b": 175}
    assert device.busy_us == 175


def test_contention_is_extrapolated_and_background_multiplies() -> None:
    contention = Contention(((1, 1.0), (2, 1.6), (4, 3.0)), background=2.0)
    assert contention.factor(1, background=False) == 1.0
    assert contention.factor(3, background=False) == pytest.approx(2.3)
    assert contention.factor(6, background=False) == pytest.approx(4.4)
    assert contention.factor(2, background=True) == pytest.approx(3.2)
    assert Contention().factor(3, background=False) == 3.0  # unmeasured: plain time-slicing


def scenario(tmp_path: Path, robots: int, latency: LatencyModel, **options: object) -> Scenario:
    files = write_files(tmp_path, contracts())
    names = tuple(f"r{i}" for i in range(robots))
    return Scenario(
        groups=(
            GroupSpec("act", files.binding, "dev-0", latency, names, Policy.DEADLINE_FAIRNESS),
        ),
        starts_us={n: i * SECOND // robots for i, n in enumerate(names)},
        warmup_us=3 * SECOND,
        duration_us=20 * SECOND,
        **options,  # type: ignore[arg-type]
    )


def test_a_fast_model_serves_robots_without_misses(tmp_path: Path) -> None:
    result = simulate(scenario(tmp_path, 8, FAST))
    assert result.robots == 8
    assert result.requests >= 8 * 18  # about one request per robot per second
    assert result.meets(Slo())
    assert result.gpu_hours_per_robot_hour == pytest.approx(1 / 8)
    assert result.age_at_dispatch_p99_ms is not None
    assert result.age_at_dispatch_p99_ms < 50


def test_an_overloaded_device_starves_robots(tmp_path: Path) -> None:
    result = simulate(scenario(tmp_path, 8, SLOW))
    assert not result.meets(Slo())
    assert result.starvations_per_robot_hour > 0 or result.misses_per_1000 > 0


def test_a_background_job_costs_capacity(tmp_path: Path) -> None:
    calm = simulate(scenario(tmp_path, 4, FAST))
    busy = simulate(
        scenario(
            tmp_path,
            4,
            FAST,
            contention=Contention(background=30.0),
            background=(("dev-0", 0, 60 * SECOND),),
        )
    )
    assert calm.meets(Slo())
    assert not busy.meets(Slo())


def test_plans_match_the_configurations() -> None:
    config = suite.PRESETS["smoke"]
    shared = suite.plan(suite.SAME_WEIGHTS, suite.NEURA_DEADLINE, 6, config)
    assert shared.groups == (("shared", "act", 6),)
    assert shared.max_batch == config.max_batch
    dedicated = suite.plan(suite.SAME_WEIGHTS, suite.DEDICATED_SHARED, 3, config)
    assert [g[2] for g in dedicated.groups] == [1, 1, 1]
    assert (dedicated.devices, dedicated.max_batch) == ("one", 1)
    assert suite.plan(suite.SAME_WEIGHTS, suite.DEDICATED, 3, config).devices == "each"
    mixed = suite.plan(suite.DIFFERENT, suite.NEURA_CO_LOCATED, 5, config)
    assert mixed.groups == (("g-act", "act", 3), ("g-diffusion", "diffusion", 2))


def test_capacity_is_the_last_count_before_the_first_failure() -> None:
    from neura_bench.suite import Cell

    def cell(robots: int, misses: float, configuration: str = suite.NEURA_DEADLINE) -> Cell:
        result = simulate_free_result(robots, misses)
        return Cell(suite.SAME_WEIGHTS, suite.PHASED, configuration, robots, result)

    cells = [cell(1, 0), cell(2, 0), cell(4, 5.0), cell(8, 0), cell(1, 0, suite.DEDICATED)]
    capacity = suite.capacity_at_slo(cells, Slo())[suite.SAME_WEIGHTS][suite.PHASED]
    assert capacity[suite.NEURA_DEADLINE] == {
        "first_failure": 4,
        "robots_per_device": 2,
        "gpu_hours_per_robot_hour": 0.5,
    }
    assert capacity[suite.DEDICATED]["robots_per_device"] == 1


def simulate_free_result(robots: int, misses: float) -> Result:
    from neura_bench.metrics import Window, summarize

    window = Window(duration_s=60, robots=robots, devices=1, requests=1000)
    window.served_late = int(misses)
    return summarize(window)


def test_per_robot_baselines_get_a_batch_group_each_at_any_count(tmp_path: Path) -> None:
    """One process per robot needs as many distinct artifacts as robots, past any pool size."""
    files = write_files(tmp_path, contracts())
    models = suite.Models(files, [], files, tmp_path)
    measured = SimpleNamespace(
        contention=SimpleNamespace(contention=Contention()),
        vla_contention={},
        vlas={},
        device_memory_mib=None,
    )
    builder = suite.Builder(suite.PRESETS["smoke"], models, measured)  # type: ignore[arg-type]
    builder.latency = lambda kind: FAST  # type: ignore[method-assign]
    p = suite.plan(suite.SAME_WEIGHTS, suite.DEDICATED_SHARED, 40, suite.PRESETS["smoke"])
    scenario = builder.simulation(p, suite.PHASED, 40, warmup_s=0.1, duration_s=0.1)
    assert len({g.binding.artifact_digest for g in scenario.groups}) == 40


def test_the_link_fit_reproduces_the_measured_median_and_tail() -> None:
    link = suite.fit_link(2.0, 3.0)
    base, jitter = link.base_us / 1000, link.jitter_us / 1000
    assert base + jitter / 2 == pytest.approx(2.0, abs=0.01)
    assert base + 0.99 * jitter == pytest.approx(3.0, abs=0.01)
    heavy = suite.fit_link(1.0, 9.0)  # a tail no uniform delay with a positive base can match
    assert heavy.base_us == 200
    assert heavy.base_us + heavy.jitter_us / 2 == pytest.approx(1000)


def test_chunks_the_robot_rejects_are_misses_counted_once() -> None:
    from neura_bench.metrics import Window, summarize

    window = Window(duration_s=60, robots=2, devices=1, requests=100)
    window.chunks_rejected_at_robot = 5
    assert summarize(window).deadline_misses == 5
    window.served_late = 3  # late ones are also rejected as too old: not added again
    assert summarize(window).deadline_misses == 5
    window.expired = 2
    assert summarize(window).deadline_misses == 7


def test_processes_that_do_not_fit_in_the_device_memory_miss_the_slo(tmp_path: Path) -> None:
    files = write_files(tmp_path, contracts())
    # Each worker process: 500 MiB at its peak, plus 300 MiB the driver holds for it.
    heavy = LatencyModel(FAST.points, process_overhead_mib=300)

    def processes(n: int) -> Scenario:
        bindings = suite._bindings(files, 0, n)  # one artifact, so one process, each
        groups = tuple(
            GroupSpec(f"g{i}", binding, "dev-0", heavy, (f"r{i}",), Policy.FIFO, 1)
            for i, binding in enumerate(bindings)
        )
        return Scenario(groups=groups, warmup_us=SECOND, duration_us=5 * SECOND)

    two, three = simulate(processes(2)), simulate(processes(3))
    assert (two.peak_memory_mib, three.peak_memory_mib) == (1600, 2400)
    device = Slo(max_memory_mib=2000)
    assert two.meets(device)
    assert not three.meets(device)
    assert three.meets(Slo())  # memory unchecked (a CPU host)


def test_a_vla_case_runs_its_model_in_batches_that_fit() -> None:
    config = suite.PRESETS["gpu"]
    p = suite.plan(suite.VLA_CASES["pi0"], suite.NEURA_DEADLINE, 8, config, max_batch=4)
    assert p.groups == (("shared", "pi0", 8),)
    assert p.max_batch == 4
    one_each = suite.plan(suite.VLA_CASES["smolvla"], suite.DEDICATED_SHARED, 3, config)
    assert [kind for _, kind, _ in one_each.groups] == ["smolvla"] * 3


def test_cases_of_models_that_were_not_measured_are_left_out(tmp_path: Path) -> None:
    files = write_files(tmp_path, contracts())
    models = suite.Models(files, [], files, tmp_path)
    measured = SimpleNamespace(
        contention=SimpleNamespace(contention=Contention()),
        vla_contention={},
        vlas={"smolvla": SimpleNamespace(largest_batch=4)},
        device_memory_mib=24_000,
    )
    builder = suite.Builder(suite.PRESETS["gpu"], models, measured)  # type: ignore[arg-type]
    cases = builder.cases()
    assert suite.VLA_CASES["smolvla"] in cases
    assert suite.VLA_CASES["pi0"] not in cases
    assert builder.max_batch(suite.VLA_CASES["smolvla"]) == 4
    assert builder.max_batch(suite.SAME_WEIGHTS) == suite.PRESETS["gpu"].max_batch
    assert builder.config.slo.max_memory_mib == 24_000


async def test_latency_stops_at_the_first_batch_that_does_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    from neura_adapters import PolicyError
    from neura_bench import measure

    class Backend:
        peak_memory_mib = 0

        async def infer(self, batch_id: int, batch: list[object], timeout_s: float) -> None:
            if len(batch) >= 4:
                raise PolicyError("CUDA out of memory", out_of_memory=True)
            self.peak_memory_mib = 100 * len(batch)

        def status(self) -> dict[str, object]:
            return {"pid": 4242}

    @contextlib.asynccontextmanager
    async def workers(*_: object, **__: object):  # type: ignore[no-untyped-def]
        yield [Backend()]

    monkeypatch.setattr(measure, "workers", workers)
    # The driver accounts the worker 350 MiB at a batch of 2, whose peak allocation is 200.
    monkeypatch.setattr(measure, "process_memory_mib", lambda pid: 350 if pid == 4242 else None)
    files = write_files(tmp_path, contracts())
    measured = await measure.latency(
        files, "cuda:0", batch_sizes=(1, 2, 4, 8), seconds=0.0, minimum=2
    )
    assert [p.batch_size for p in measured.model.points] == [1, 2]
    assert measured.out_of_memory_at == 4
    assert measured.largest_batch == 2
    assert measured.model.process_overhead_mib == 150


def test_robots_of_a_language_conditioned_model_arm_with_an_instruction(tmp_path: Path) -> None:
    files = write_files(tmp_path, contracts(model="smolvla"))
    assert files.binding.language_input is not None
    names = ("r0", "r1")
    result = simulate(
        Scenario(
            groups=(
                GroupSpec("vla", files.binding, "dev-0", FAST, names, Policy.DEADLINE_FAIRNESS, 4),
            ),
            warmup_us=SECOND,
            duration_us=5 * SECOND,
        )
    )
    assert result.requests > 0  # armed and served
    assert result.faults_per_robot_hour == 0


def test_a_failed_real_time_run_is_recorded_and_the_others_go_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fails(_: object) -> None:
        raise RuntimeError("the worker could not start")

    monkeypatch.setattr(realtime, "run", fails)
    builder = SimpleNamespace(
        config=suite.PRESETS["smoke"],
        measured=SimpleNamespace(vlas={}),
        max_batch=lambda case: 4,
        realtime=lambda plan, workload: None,
    )
    failures: dict[str, str] = {}
    out = asyncio.run(suite.validate(builder, progress=lambda _: None, failures=failures))  # type: ignore[arg-type]
    assert out == []
    planned = suite.validation_plan(suite.PRESETS["smoke"])
    assert len(failures) == len(planned)
    assert failures["same_weights/phased/dedicated_gpu N=1"] == (
        "RuntimeError: the worker could not start"
    )
    with pytest.raises(RuntimeError):  # without a record to keep, a failure still stops the run
        asyncio.run(suite.validate(builder, progress=lambda _: None))  # type: ignore[arg-type]


def test_a_vla_timing_makes_a_profile_a_slow_pass_can_meet(tmp_path: Path) -> None:
    timing = VlaTiming()
    files = write_files(tmp_path, contracts(model="pi0", vla_timing=timing))
    spec = files.binding.profile.spec
    assert spec.chunk.actions_per_chunk == 50
    assert spec.chunk.refill_threshold_actions == 30
    assert spec.freshness.max_observation_age_at_dispatch_us == 600_000
    assert spec.freshness.max_observation_age_at_acceptance_us == 1_500_000
    assert spec.freshness.request_timeout_us == 1_500_000  # a request outlives a 0.4 s pass
    act = write_files(tmp_path / "act", contracts(model="act", vla_timing=timing))
    assert act.binding.profile.spec.freshness.max_observation_age_at_acceptance_us == 150_000


async def test_contention_refuses_more_workers_than_it_was_given(tmp_path: Path) -> None:
    from neura_bench import measure

    files = write_files(tmp_path, contracts())
    with pytest.raises(ValueError, match="4 workers asked for, 2 deployments given"):
        await measure.contention([files, files], "cpu", seconds=0.0, counts=(1, 4))


def test_the_vla_preset_runs_only_its_cases_with_each_models_contention(tmp_path: Path) -> None:
    config = suite.PRESETS["vla"]
    files = write_files(tmp_path, contracts())
    models = suite.Models(files, [], files, tmp_path)
    act = Contention(((1, 1.0), (24, 6.0)))
    pi0 = Contention(((1, 1.0), (2, 1.9)))
    measured = SimpleNamespace(
        contention=SimpleNamespace(contention=act),
        vla_contention={"pi0": SimpleNamespace(contention=pi0)},
        vlas={
            "smolvla": SimpleNamespace(largest_batch=16),
            "pi0": SimpleNamespace(largest_batch=8),
        },
        device_memory_mib=23_000,
    )
    builder = suite.Builder(config, models, measured)  # type: ignore[arg-type]
    assert set(builder.cases()) == {suite.SAME_WEIGHTS, *suite.VLA_CASES.values()}
    assert builder.contention({"pi0"}) is pi0
    assert builder.contention({"smolvla"}) is act  # not measured: ACT's
    assert builder.contention({"act", "pi0"}) is act
    plan = suite.validation_plan(config, ["smolvla", "pi0"])
    assert {case for case, *_ in plan} == {suite.SAME_WEIGHTS, *suite.VLA_CASES.values()}
    # One process per robot in real time: 16 for ACT, 2 of each VLA.
    assert (suite.SAME_WEIGHTS, suite.PHASED, suite.DEDICATED_SHARED, 16) in plan
    for case in suite.VLA_CASES.values():
        assert (case, suite.PHASED, suite.DEDICATED_SHARED, 2) in plan
        assert (case, suite.PHASED, suite.NEURA_DEADLINE, 4) in plan
    assert max(config.contention_counts) == config.contention_workers
