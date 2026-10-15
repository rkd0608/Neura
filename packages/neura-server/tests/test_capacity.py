"""Capacity estimates come from the gateway's own admission model (design §9.3)."""

from __future__ import annotations

from typing import Any

from neura_server.admission import AdmissionController, SessionDemand
from neura_server.capacity import estimate
from neura_server.config import ProfilePointConfig, ServingConfig
from neura_server.profiles import LatencyProfile


def test_estimate_matches_what_admission_would_admit(make_binding: Any) -> None:
    binding = make_binding()
    serving = ServingConfig()
    result = estimate(binding, serving, rtt_p99_ms=20)
    n = result["max_robots"]
    assert n > 0
    assert result["source"] == "configured_profile"
    # Exactly n identical robots are admitted by the controller the gateway runs; n + 1 are not.
    admission = AdmissionController(
        LatencyProfile(serving.profile_points()),
        max_batch=serving.max_batch,
        reserve_fraction=serving.reserve_fraction,
    )
    demand = SessionDemand.from_binding("r", binding, rtt_p99_us=20_000)
    for i in range(n):
        assert admission.try_admit(
            SessionDemand(
                f"r{i}",
                demand.budget_us,
                demand.interval_us,
                demand.link_us,
                demand.max_age_at_dispatch_us,
                demand.max_age_at_acceptance_us,
            )
        ).admitted
    refused = admission.try_admit(
        SessionDemand(
            f"r{n}",
            demand.budget_us,
            demand.interval_us,
            demand.link_us,
            demand.max_age_at_dispatch_us,
            demand.max_age_at_acceptance_us,
        )
    )
    assert not refused.admitted
    assert refused.limit == result["limiting_factor"]
    assumptions = result["assumptions"]
    assert assumptions["control_period_ms"] == binding.control_period_us / 1000
    assert assumptions["latency_p99_ms_by_batch"] == {"1": 25.0, "8": 45.0}


def test_slower_links_and_less_memory_lower_the_estimate(make_binding: Any) -> None:
    binding = make_binding()
    fast = estimate(binding, ServingConfig(), rtt_p99_ms=5)["max_robots"]
    slow = estimate(binding, ServingConfig(), rtt_p99_ms=60)["max_robots"]
    assert slow <= fast
    tight = ServingConfig(
        memory_available_mib=1000,
        profile=[
            ProfilePointConfig(batch_size=1, p50_ms=18, p99_ms=25, memory_mib=900),
            ProfilePointConfig(batch_size=8, p50_ms=32, p99_ms=45, memory_mib=1400),
        ],
    )
    limited = estimate(binding, tight, rtt_p99_ms=20)
    assert limited["limiting_factor"] == "memory"
    assert limited["max_robots"] < fast
