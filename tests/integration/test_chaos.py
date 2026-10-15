"""Real-process chaos in CI (``neura-chaos``, design §21): a gateway process with its policy
worker processes, behind a fault proxy, serving robots over gRPC while workers are killed and
hung, the gateway is killed, frozen, restarted and reloaded, and the network stalls, drops and
delays. Every robot is watched by the independent invariant monitor. The long runs are in the
soak workflow."""

from __future__ import annotations

from pathlib import Path

from neura_sim.chaos import FAULTS, Chaos, ChaosConfig

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "contracts"


async def test_short_chaos_run_has_no_violations_and_recovers() -> None:
    config = ChaosConfig(
        duration_s=45,
        robots=3,
        seed=3,
        mean_fault_interval_s=2.5,
        cooldown_s=10,
        contracts=EXAMPLES,
    )
    report = await Chaos(config).run()
    assert report["invariants"]["violations"] == 0, report["invariants"]
    assert report["passed"], report
    assert report["faults_total"] >= 8
    assert len(report["faults_injected"]) >= 5, report["faults_injected"]
    assert set(report["faults_injected"]) <= set(FAULTS)
    assert report["invariants"]["checked"]["chunks_accepted"] > 50
