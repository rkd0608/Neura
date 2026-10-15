"""Capacity estimates: "this server can run N robots at this profile" (design §9.3, §19.3).

Computed with the admission controller the gateway enforces, for robots identical to the given
binding, so the estimate and the gateway's admission decisions cannot disagree. The latency
profile is the configured one until the profiler measures it on the site's GPU (M6).
"""

from __future__ import annotations

from typing import Any

from neura_runtime.binding import ExecutionBinding
from neura_server.admission import AdmissionController, SessionDemand
from neura_server.config import ServingConfig
from neura_server.profiles import LatencyProfile


def estimate(
    binding: ExecutionBinding, serving: ServingConfig, *, rtt_p99_ms: float
) -> dict[str, Any]:
    """The estimate as a JSON object (api/openapi.yaml CapacityEstimate, without estimated_at)."""
    profile = LatencyProfile(serving.profile_points())
    admission = AdmissionController(
        profile,
        max_batch=serving.max_batch,
        reserve_fraction=serving.reserve_fraction,
        memory_available_mib=serving.memory_available_mib,
    )
    demand = SessionDemand.from_binding("template", binding, rtt_p99_us=int(rtt_p99_ms * 1000))
    report = admission.capacity(demand)
    freshness = binding.profile.spec.freshness

    def ms(us: int) -> float:
        return round(us / 1000, 3)

    return {
        "max_robots": report.max_sessions,
        "limiting_factor": report.limiting_factor,
        "source": "configured_profile",
        "assumptions": {
            "control_period_ms": ms(binding.control_period_us),
            "starvation_budget_ms": ms(demand.budget_us),
            "request_interval_ms": ms(demand.interval_us),
            "link_allowance_ms": ms(demand.link_us),
            "dispatch_freshness_ms": ms(freshness.max_observation_age_at_dispatch_us),
            "acceptance_freshness_ms": ms(freshness.max_observation_age_at_acceptance_us),
            "max_batch": serving.max_batch,
            "reserve_fraction": serving.reserve_fraction,
            "latency_p99_ms_by_batch": {
                str(point.batch_size): float(point.p99_ms) for point in serving.profile
            },
            "memory_available_mib": serving.memory_available_mib,
        },
    }
