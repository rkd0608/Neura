"""``neura-qualify``: the fault suite and parity on the emulated arm, the closed-loop statistics,
and the rules for promoting a combination to Supported."""

from __future__ import annotations

import asyncio
import json
import socket
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from neura_bench.qualify.cli import promotion_problems
from neura_bench.qualify.episodes import Episode, Episodes, EpisodesConfig, schedule
from neura_bench.qualify.harness import Qualification, QualifyConfig, ScenarioResult
from neura_bench.qualify.protocol import MARGIN
from neura_contracts.stats import difference_unpaired
from neura_robots.so101 import So101Config

ROOT = Path(__file__).resolve().parents[3]


def test_the_default_protocol_can_show_non_inferiority() -> None:
    def lower(s1: int, n1: int, s2: int, n2: int) -> float:
        return difference_unpaired(s1, n1, s2, n2)[1]

    assert lower(27, 30, 27, 30) > -MARGIN  # 30 episodes each at 90%: passes
    assert lower(10, 10, 10, 10) < -MARGIN  # 10 each cannot, even at 100%
    assert lower(20, 30, 27, 30) < -MARGIN  # a real drop fails


def test_episodes_alternate_in_balanced_blocks() -> None:
    order = schedule(10, seed=3)
    assert Counter(order) == {"neura": 10, "local": 10}
    for block in range(0, 20, 4):
        a, b, c, d = order[block : block + 4]
        assert a == d
        assert b == c
        assert a != b


STARTED = datetime(2026, 10, 15, 10, tzinfo=UTC).timestamp()


def _setup() -> dict[str, Any]:
    """A real arm (no emulator) and the example contracts."""
    contracts = ROOT / "examples" / "contracts"
    return {
        "arm": So101Config(port="/dev/ttyACM0", calibration_id="arm-1"),
        "artifact": contracts / "act-so101.artifact.yaml",
        "robot_profile": contracts / "so101.robot.yaml",
        "execution_profile": contracts / "act-so101-30hz.profile.yaml",
        "checkpoint": Path("checkpoint"),
    }


def _on_hardware(**config: Any) -> dict[str, Any]:
    """The report of a run on a real arm in which every scenario the harness chose passed, as
    are parity and the global checks: what a passing hardware run writes."""
    run = Qualification(QualifyConfig(**_setup(), **{"manual": True, **config}))
    for name, _ in run.applicable():
        result = ScenarioResult(name)
        result.check("passed", True)
        run.results.append(result)
    run.parity = {"measured": True, "passed": True, "max_abs_error": 0.0}
    return run.report(STARTED, STARTED + 600)


def _closed_loop(**config: Any) -> dict[str, Any]:
    """The closed-loop report of a run on the same arm and contracts in which every episode,
    of both arms, succeeded."""
    run = Episodes(EpisodesConfig(**_setup(), **config))
    order = schedule(run.config.pairs, run.config.seed)
    run.episodes = [Episode(i, arm, True, 10.0, 300) for i, arm in enumerate(order, 1)]
    return run.report(STARTED)


def _report(**overrides: Any) -> dict[str, Any]:
    report = _on_hardware()
    report.update(overrides)
    return report


def _episodes(**overrides: Any) -> dict[str, Any]:
    episodes = _closed_loop()
    episodes.update(overrides)
    return episodes


def test_a_complete_run_on_hardware_is_promoted() -> None:
    """A servo overload is induced only on the emulator: a hardware run that passed every
    other scenario is complete, and promoted."""
    report = _report()
    skipped = {s["name"]: s["skipped"] for s in report["scenarios"] if s["skipped"]}
    assert list(skipped) == ["torque_loss"]
    assert report["complete"]
    assert promotion_problems(report, _episodes()) == []


def test_only_a_complete_hardware_pass_is_promoted() -> None:
    assert "emulated" in " ".join(promotion_problems(_report(emulated=True), _episodes()))
    assert promotion_problems(_report(passed=False), _episodes())
    partial = _report(scenarios=[{"name": "nominal", "skipped": None}])
    assert "did not run" in " ".join(promotion_problems(partial, _episodes()))
    without_manual = _on_hardware(manual=False)
    assert not without_manual["complete"]
    assert "bus_unplugged" in " ".join(promotion_problems(without_manual, _episodes()))
    assert promotion_problems(_report(), _episodes(verdict={"non_inferior": False}))
    assert promotion_problems(_report(), _episodes(contracts={"artifact": "sha256:b"}))
    assert promotion_problems(_report(), _episodes(emulated=True))


def test_a_protocol_looser_than_the_defaults_is_not_promoted() -> None:
    report = _on_hardware(hold_tolerance=45.0, parity_atol=1.0, nominal_s=2.0)
    episodes = _closed_loop(pairs=20, margin=0.95, blinded=False)
    episodes["contracts"]["execution_profile"] = "sha256:c"
    assert report["passed"]  # each by its own protocol
    assert episodes["verdict"]["non_inferior"]
    assert promotion_problems(report, episodes) == [
        "the episodes ran a different execution profile",
        "the qualification ran with --hold-tolerance 45: promotion needs at most 1",
        "the qualification ran with --parity-atol 1: promotion needs at most 0.001",
        "the qualification ran with --nominal-seconds 2: promotion needs at least 20",
        "the episodes ran with --pairs 20: promotion needs at least 30",
        "the episodes ran with --margin 0.95: promotion needs at most 0.15",
        "the episodes were not blinded (--unblinded)",
    ]
    stricter = _on_hardware(hold_tolerance=0.5, parity_atol=1e-4, nominal_s=60.0)
    assert promotion_problems(stricter, _closed_loop(pairs=40, margin=0.10)) == []
    del report["protocol"]  # written before reports recorded it
    assert "does not record its --hold-tolerance" in " ".join(promotion_problems(report, episodes))


def test_promote_writes_the_supported_entry(tmp_path: Path) -> None:
    from neura_bench.qualify import cli

    report = _report(
        contracts={
            **_report()["contracts"],
            "model": "act-so101",
            "robot": "so101-follower",
            "execution_profile_name": "act-so101-30hz",
            "device": "cuda:0",
        }
    )
    (tmp_path / "r.json").write_text(json.dumps(report))
    (tmp_path / "e.json").write_text(json.dumps(_episodes()))
    supported = tmp_path / "supported.json"
    supported.write_text("[]")
    args = [
        "promote",
        "--report",
        str(tmp_path / "r.json"),
        "--episodes",
        str(tmp_path / "e.json"),
        "--gpu",
        "RTX 4090",
        "--supported",
        str(supported),
    ]
    assert cli.main([*args, "--evidence", "http://insecure"]) == 1
    assert cli.main([*args, "--evidence", "https://example.org/qualification/1"]) == 0
    assert cli.main([*args, "--evidence", "https://example.org/qualification/2"]) == 0
    rows = json.loads(supported.read_text())
    assert rows == [
        {
            "model": "act-so101",
            "robot": "so101-follower",
            "execution_profile": "act-so101-30hz",
            "gpu": "RTX 4090",
            "qualified_on": "2026-10-15",
            "evidence": "https://example.org/qualification/2",
        }
    ]


def test_a_site_that_cannot_start_leaves_nothing_running(tmp_path: Path) -> None:
    from neura_bench.qualify.site import LocalSite

    contracts = ROOT / "examples" / "contracts"
    site = LocalSite(
        contracts / "act-so101.artifact.yaml",
        contracts / "so101.robot.yaml",
        contracts / "act-so101-30hz.profile.yaml",
        None,
        "cpu",
        tmp_path / "site",
    )

    async def start() -> None:
        with socket.socket() as taken:  # another program has the proxy's port
            taken.bind(("127.0.0.1", site.proxy.listen_port))
            taken.listen()
            with pytest.raises(OSError, match="address already in use"):
                async with site:
                    pass
        supervisor = site.gateway._task
        assert supervisor is None or supervisor.done(), "a gateway would start after the error"
        assert site.gateway.pid is None

    asyncio.run(start())


# --- on the emulated arm (needs LeRobot) ----------------------------------------------------------

FAST = (
    "nominal",
    "partition",
    "operator_takeover",
    "invalid_action",
    "controller_failure",
    "torque_loss",
    "second_runtime_blocked",
    "actuator_lock_lost",
)


def test_the_fault_suite_and_parity_on_the_emulated_arm(tmp_path: Path) -> None:
    pytest.importorskip("lerobot", reason="needs the models group: uv sync --group models")
    from neura_bench.qualify.cli import emulated
    from neura_bench.qualify.harness import Qualification, QualifyConfig

    with emulated(tmp_path) as setup:
        config = QualifyConfig(
            **setup, scenarios=FAST, nominal_s=4.0, parity_samples=4, workdir=tmp_path / "site"
        )
        report = asyncio.run(Qualification(config).run())

    by_name = {s["name"]: s for s in report["scenarios"]}
    failures = {
        name: [c for c in s["checks"] if not c["passed"]]
        for name, s in by_name.items()
        if not s["passed"]
    }
    assert failures == {}, failures
    assert report["parity"]["passed"], report["parity"]
    assert report["parity"]["samples"] == 4
    assert report["invariants"]["violations"] == 0
    assert report["emulated"] is True
    assert report["complete"] is False  # a subset: never promotable
    assert promotion_problems(report, _episodes())
    torque = by_name["torque_loss"]["observed"]["fault"]
    assert torque["confirmed"] is False  # a stop that cannot be confirmed is never claimed
    assert by_name["partition"]["observed"]["fault"]["reason"] == "STARVATION_GRACE_EXHAUSTED"


def test_closed_loop_episodes_on_the_emulated_arm(tmp_path: Path) -> None:
    pytest.importorskip("lerobot", reason="needs the models group: uv sync --group models")
    from neura_bench.qualify.cli import emulated
    from neura_bench.qualify.episodes import Episodes, EpisodesConfig

    with emulated(tmp_path) as setup:
        setup.pop("emulator")
        config = EpisodesConfig(
            **setup,
            pairs=2,
            seconds=1.5,
            emulated=True,
            workdir=tmp_path / "site",
            score=lambda i, arm: arm is None,  # blinded: the operator is never told the arm
        )
        report = asyncio.run(Episodes(config).run())
    episodes = report["episodes"]
    assert Counter(e["arm"] for e in episodes) == {"neura": 2, "local": 2}
    assert all(e["success"] for e in episodes)
    assert all(e["actions"] >= 30 for e in episodes), episodes  # both at the control rate
    assert all(e["fault"] is None for e in episodes), episodes
    assert report["verdict"]["complete"]
    assert not report["verdict"]["non_inferior"]  # 2 episodes each prove nothing
