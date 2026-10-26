"""``neura-qualify``: the fault suite and parity on the emulated arm, the closed-loop statistics,
and the rules for promoting a combination to Supported."""

from __future__ import annotations

import asyncio
import json
import socket
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from neura_robots.qualify.cli import promotion_problems
from neura_robots.qualify.episodes import newcombe_lower, schedule, wilson
from neura_robots.qualify.harness import SCENARIOS

ROOT = Path(__file__).resolve().parents[3]


def test_wilson_and_newcombe_match_the_published_example() -> None:
    # Newcombe (1998), Statistics in Medicine 17:873, example (a): 56/70 vs 48/80, method 10.
    z = 1.959963984540054
    assert newcombe_lower(56, 70, 48, 80, z) == pytest.approx(0.0524, abs=1e-4)
    low, high = wilson(56, 70, z)
    assert (low, high) == pytest.approx((0.6918, 0.8770), abs=1e-4)  # the closed form


def test_the_default_protocol_can_show_non_inferiority() -> None:
    assert newcombe_lower(27, 30, 27, 30) > -0.15  # 30 episodes each at 90%: passes
    assert newcombe_lower(10, 10, 10, 10) < -0.15  # 10 each cannot, even at 100%
    assert newcombe_lower(20, 30, 27, 30) < -0.15  # a real drop fails


def test_episodes_alternate_in_balanced_blocks() -> None:
    order = schedule(10, seed=3)
    assert Counter(order) == {"neura": 10, "local": 10}
    for block in range(0, 20, 4):
        a, b, c, d = order[block : block + 4]
        assert a == d
        assert b == c
        assert a != b


def _report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "kind": "neura-hardware-qualification",
        "emulated": False,
        "passed": True,
        "complete": True,
        "started_at": "2026-10-15T10:00:00Z",
        "scenarios": [{"name": n, "skipped": None, "passed": True} for n in SCENARIOS],
        "contracts": {"artifact": "sha256:a"},
    }
    report.update(overrides)
    return report


def _episodes(**overrides: Any) -> dict[str, Any]:
    episodes: dict[str, Any] = {
        "kind": "neura-closed-loop-episodes",
        "emulated": False,
        "contracts": {"artifact": "sha256:a"},
        "verdict": {"non_inferior": True},
    }
    episodes.update(overrides)
    return episodes


def test_only_a_complete_hardware_pass_is_promoted() -> None:
    assert promotion_problems(_report(), _episodes()) == []
    assert "emulated" in " ".join(promotion_problems(_report(emulated=True), _episodes()))
    assert promotion_problems(_report(passed=False), _episodes())
    partial = _report(scenarios=[{"name": "nominal", "skipped": None}])
    assert "did not run" in " ".join(promotion_problems(partial, _episodes()))
    skipped = _report(
        scenarios=[
            {"name": n, "skipped": "manual" if n == "bus_unplugged" else None} for n in SCENARIOS
        ]
    )
    assert "bus_unplugged" in " ".join(promotion_problems(skipped, _episodes()))
    assert promotion_problems(_report(), _episodes(verdict={"non_inferior": False}))
    assert promotion_problems(_report(), _episodes(contracts={"artifact": "sha256:b"}))
    assert promotion_problems(_report(), _episodes(emulated=True))


def test_promote_writes_the_supported_entry(tmp_path: Path) -> None:
    from neura_robots.qualify import cli

    report = _report(
        contracts={
            "artifact": "sha256:a",
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
    from neura_robots.qualify.site import LocalSite

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
    from neura_robots.qualify.cli import emulated
    from neura_robots.qualify.harness import Qualification, QualifyConfig

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
    from neura_robots.qualify.cli import emulated
    from neura_robots.qualify.episodes import Episodes, EpisodesConfig

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
    assert all(e["fault"] is None for e in episodes)
    assert report["verdict"]["complete"]
    assert not report["verdict"]["non_inferior"]  # 2 episodes each prove nothing
