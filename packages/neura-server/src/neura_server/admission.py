"""Admission control: refuse a session the admitted load could not serve in time (design §9.3).

The check is deliberately conservative and explainable. For the burst case (every session asks at
the same moment) it assumes one non-preemptible batch of the other sessions is already running,
serves sessions in deadline order in batches of at most ``max_batch``, and requires each session's
predicted completion plus its link allowance to fit inside its starvation budget. It also bounds
average utilization below ``1 - reserve_fraction`` and checks GPU memory headroom.
"""

from __future__ import annotations

from dataclasses import dataclass

from neura_contracts import ReasonCode
from neura_runtime.binding import ExecutionBinding
from neura_server.profiles import LatencyProfile


@dataclass(frozen=True, slots=True)
class SessionDemand:
    session_id: str
    # Time from the robot's refill request until its queue starves (design §8).
    budget_us: int
    # Mean time between this session's requests.
    interval_us: int
    # Allowance for uplink plus downlink; twice the measured p99 RTT by default.
    link_us: int
    # Largest observation age allowed at dispatch and at acceptance (execution profile).
    max_age_at_dispatch_us: int = 2**62
    max_age_at_acceptance_us: int = 2**62

    @classmethod
    def from_binding(
        cls,
        session_id: str,
        binding: ExecutionBinding,
        *,
        rtt_p99_us: int,
        safety_margin_us: int | None = None,
    ) -> SessionDemand:
        spec = binding.profile.spec
        period = binding.control_period_us
        margin = period if safety_margin_us is None else safety_margin_us
        budget = spec.chunk.refill_threshold_actions * period - margin
        interval = max(
            period,
            (spec.chunk.actions_per_chunk - spec.chunk.refill_threshold_actions) * period,
        )
        return cls(
            session_id,
            max(budget, 0),
            interval,
            2 * rtt_p99_us,
            spec.freshness.max_observation_age_at_dispatch_us,
            spec.freshness.max_observation_age_at_acceptance_us,
        )


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    admitted: bool
    reason: ReasonCode | None
    detail: str
    worst_case_completion_us: int
    utilization: float
    # What refused the load: memory, dispatch_freshness, acceptance_freshness, deadline or
    # throughput. None when admitted.
    limit: str | None = None


@dataclass(frozen=True, slots=True)
class CapacityReport:
    max_sessions: int
    limiting_factor: str  # the limit that refused one more session, or "search_limit"


class AdmissionController:
    def __init__(
        self,
        profile: LatencyProfile,
        *,
        max_batch: int,
        reserve_fraction: float = 0.2,
        memory_available_mib: int | None = None,
        batch_window_us: int = 0,
        lanes: int = 1,
    ) -> None:
        if max_batch < 1:
            raise ValueError("max_batch must be >= 1")
        if lanes < 1:
            raise ValueError("lanes must be >= 1")
        if not 0.0 <= reserve_fraction < 1.0:
            raise ValueError("reserve_fraction must be in [0, 1)")
        self._profile = profile
        self._max_batch = max_batch
        self._reserve = reserve_fraction
        self._memory = memory_available_mib
        self._window = batch_window_us
        # Forward passes at once (§9.2): a worker process, and its own copy of the weights, each.
        # How much passes running together slow each other is the device's and is not modeled
        # here; the scheduler's profile learns it from the passes it times.
        self._lanes = lanes
        self._admitted: dict[str, SessionDemand] = {}

    @property
    def admitted(self) -> dict[str, SessionDemand]:
        return dict(self._admitted)

    def evaluate(self, demands: list[SessionDemand]) -> AdmissionDecision:
        if not demands:
            return AdmissionDecision(True, None, "no sessions", 0, 0.0)
        n = len(demands)
        typical_batch = min(n, self._max_batch)
        if self._memory is not None:
            needed = self._lanes * self._profile.memory_mib(typical_batch)
            if needed > self._memory:
                return AdmissionDecision(
                    False,
                    ReasonCode.ADMISSION_MEMORY_EXCEEDED,
                    f"{self._lanes} worker(s) with batches of {typical_batch} need {needed} MiB, "
                    f"{self._memory} MiB available",
                    0,
                    0.0,
                    "memory",
                )

        # Every lane busy with a batch of the other sessions (a session has at most one request
        # outstanding, so never its own), unless there are fewer of them than lanes, and the
        # scheduler's batching window before the next. Then batches, a lane each, in rounds.
        others = n - 1
        per_lane = min(-(-others // self._lanes), self._max_batch)
        busy = others >= self._lanes
        blocking = (self._profile.p99_us(per_lane) if busy else 0) + self._window
        ordered = sorted(demands, key=lambda d: d.budget_us - d.link_us)
        batches = [
            ordered[start : start + self._max_batch] for start in range(0, n, self._max_batch)
        ]
        dispatched = blocking  # when the current round of batches starts, a lane each
        worst = 0
        for first in range(0, len(batches), self._lanes):
            ends = []
            for members in batches[first : first + self._lanes]:
                elapsed = dispatched + self._profile.p99_us(len(members))
                ends.append(elapsed)
                for demand in members:
                    uplink = demand.link_us // 2
                    completion = elapsed + demand.link_us
                    worst = max(worst, completion)
                    problem = None
                    limit = "deadline"
                    if dispatched + uplink > demand.max_age_at_dispatch_us:
                        limit = "dispatch_freshness"
                        problem = (
                            f"observation age at dispatch {dispatched + uplink} us exceeds "
                            f"{demand.max_age_at_dispatch_us} us"
                        )
                    elif completion > demand.max_age_at_acceptance_us:
                        limit = "acceptance_freshness"
                        problem = (
                            f"observation age at acceptance {completion} us exceeds "
                            f"{demand.max_age_at_acceptance_us} us"
                        )
                    elif completion > demand.budget_us:
                        problem = (
                            f"predicted completion {completion} us exceeds the "
                            f"{demand.budget_us} us starvation budget"
                        )
                    if problem is not None:
                        return AdmissionDecision(
                            False,
                            ReasonCode.ADMISSION_CAPACITY_EXCEEDED,
                            f"{n} sessions: {problem} for {demand.session_id}",
                            worst,
                            0.0,
                            limit,
                        )
            dispatched = max(ends)

        per_request = self._profile.p99_us(typical_batch) / typical_batch
        utilization = sum(per_request / d.interval_us for d in demands) / self._lanes
        if utilization > 1.0 - self._reserve:
            return AdmissionDecision(
                False,
                ReasonCode.ADMISSION_CAPACITY_EXCEEDED,
                f"utilization {utilization:.2f} exceeds {1.0 - self._reserve:.2f} "
                f"(reserve {self._reserve:.0%})",
                worst,
                utilization,
                "throughput",
            )
        return AdmissionDecision(True, None, "admitted", worst, utilization)

    def try_admit(self, demand: SessionDemand) -> AdmissionDecision:
        candidates = [d for sid, d in self._admitted.items() if sid != demand.session_id]
        decision = self.evaluate([*candidates, demand])
        if decision.admitted:
            self._admitted[demand.session_id] = demand
        return decision

    def update(self, demand: SessionDemand) -> None:
        """Replace an admitted session's demand with a better-measured one (its link, from its
        heartbeats). The session stays admitted whatever it now needs: admission refuses new load
        and never evicts (design §9.3). Later admissions plan with this demand."""
        if demand.session_id in self._admitted:
            self._admitted[demand.session_id] = demand

    def remove(self, session_id: str) -> None:
        self._admitted.pop(session_id, None)

    def capacity_estimate(self, demand_template: SessionDemand, limit: int = 1024) -> int:
        """How many identical sessions this worker could admit: 'this server can run N robots'."""
        return self.capacity(demand_template, limit).max_sessions

    def capacity(self, demand_template: SessionDemand, limit: int = 1024) -> CapacityReport:
        """The capacity estimate and what limits it, for identical sessions."""
        count = 0
        while count < limit:
            trial = [
                SessionDemand(
                    f"s{i}",
                    demand_template.budget_us,
                    demand_template.interval_us,
                    demand_template.link_us,
                    demand_template.max_age_at_dispatch_us,
                    demand_template.max_age_at_acceptance_us,
                )
                for i in range(count + 1)
            ]
            decision = self.evaluate(trial)
            if not decision.admitted:
                return CapacityReport(count, decision.limit or "deadline")
            count += 1
        return CapacityReport(count, "search_limit")
