"""The benchmark simulator's device model and metrics, and the suite's planning (no torch)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from neura_bench import suite
from neura_bench.deployments import contracts, write_files
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
    measured = SimpleNamespace(contention=SimpleNamespace(contention=Contention()))
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
