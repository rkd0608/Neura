"""Capacity estimates: "this server can run N robots at this profile" (design §9.3, §9.5, §19.3).

Computed with the admission controller the gateway enforces, for robots identical to the given
binding, so the estimate and the gateway's admission decisions cannot disagree. The latency
profile is the one measured on the site's device at prepare when there is one (`measured_profile`),
else the configured one.
"""

from __future__ import annotations

from typing import Any

from neura_runtime.binding import ExecutionBinding
from neura_server.admission import AdmissionController, SessionDemand
from neura_server.config import ServingConfig
from neura_server.profiles import LatencyProfile, ProfilePoint


def estimate(
    binding: ExecutionBinding,
    serving: ServingConfig,
    *,
    rtt_p99_ms: float,
    measured: list[ProfilePoint] | None = None,
    sharing_case: str = "same_weights",
    device: str | None = None,
    co_located_groups: int = 0,
) -> dict[str, Any]:
    """The estimate as a JSON object (api/openapi.yaml CapacityEstimate, without estimated_at).

    ``sharing_case`` is the §9.5 case for this revision's batch group on its device. With
    co-located groups the estimate assumes this group has the device to itself: contention from
    the other groups is measured by the benchmarks, not modeled here, and the assumptions say so.
    """
    points = measured if measured is not None else serving.profile_points()
    profile = LatencyProfile(points)
    admission = AdmissionController(
        profile,
        max_batch=serving.max_batch,
        reserve_fraction=serving.reserve_fraction,
        memory_available_mib=serving.memory_available_mib,
        batch_window_us=int(serving.batch_window_ms * 1000),
    )
    demand = SessionDemand.from_binding("template", binding, rtt_p99_us=int(rtt_p99_ms * 1000))
    report = admission.capacity(demand)
    freshness = binding.profile.spec.freshness

    def ms(us: int) -> float:
        return round(us / 1000, 3)

    return {
        "max_robots": report.max_sessions,
        "limiting_factor": report.limiting_factor,
        "source": "configured_profile" if measured is None else "measured_profile",
        "sharing_case": sharing_case,
        "assumptions": {
            "control_period_ms": ms(binding.control_period_us),
            "starvation_budget_ms": ms(demand.budget_us),
            "request_interval_ms": ms(demand.interval_us),
            "link_allowance_ms": ms(demand.link_us),
            "dispatch_freshness_ms": ms(freshness.max_observation_age_at_dispatch_us),
            "acceptance_freshness_ms": ms(freshness.max_observation_age_at_acceptance_us),
            "max_batch": serving.max_batch,
            "batch_window_ms": serving.batch_window_ms,
            "reserve_fraction": serving.reserve_fraction,
            "latency_p99_ms_by_batch": {
                str(point.batch_size): round(point.p99_us / 1000, 3) for point in points
            },
            "memory_available_mib": serving.memory_available_mib,
            "device": device,
            "co_located_groups": co_located_groups,
        },
    }
