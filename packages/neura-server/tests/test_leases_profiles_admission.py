from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from neura_contracts import ReasonCode
from neura_server.admission import AdmissionController, SessionDemand
from neura_server.leases import (
    Lease,
    LeaseDenied,
    LeaseTable,
    MemoryGenerationStore,
    SqliteGenerationStore,
)
from neura_server.profiles import LatencyProfile, ProfilePoint

MS = 1_000


# --- leases ------------------------------------------------------------------------------------


def test_one_live_lease_per_robot() -> None:
    table = LeaseTable(MemoryGenerationStore(), ttl_us=1_000)
    first = table.acquire("r1", 0)
    assert isinstance(first, Lease)
    second = table.acquire("r1", 500)
    assert isinstance(second, LeaseDenied)
    assert second.reason is ReasonCode.LEASE_BUSY
    assert second.holder_session_id == first.session_id


def test_expired_lease_is_replaced_with_higher_generation() -> None:
    table = LeaseTable(MemoryGenerationStore(), ttl_us=1_000)
    first = table.acquire("r1", 0)
    second = table.acquire("r1", 1_000)
    assert isinstance(first, Lease)
    assert isinstance(second, Lease)
    assert second.generation == first.generation + 1
    stale = table.check(first.session_id, first.generation, 1_001)
    assert isinstance(stale, LeaseDenied)
    assert stale.reason is ReasonCode.SESSION_UNKNOWN


def test_check_renew_and_generation_mismatch() -> None:
    table = LeaseTable(MemoryGenerationStore(), ttl_us=1_000)
    lease = table.acquire("r1", 0)
    assert isinstance(lease, Lease)
    renewed = table.renew(lease.session_id, lease.generation, 900)
    assert isinstance(renewed, Lease)
    assert renewed.expires_at_us == 1_900
    wrong = table.check(lease.session_id, lease.generation + 1, 950)
    assert isinstance(wrong, LeaseDenied)
    assert wrong.reason is ReasonCode.LEASE_GENERATION_STALE
    expired = table.check(lease.session_id, lease.generation, 1_900)
    assert isinstance(expired, LeaseDenied)
    assert expired.reason is ReasonCode.LEASE_EXPIRED


def test_release_revoke_and_sweep() -> None:
    table = LeaseTable(MemoryGenerationStore(), ttl_us=1_000)
    a = table.acquire("a", 0)
    b = table.acquire("b", 0)
    assert isinstance(a, Lease)
    assert isinstance(b, Lease)
    assert table.release(a.session_id) is a
    assert table.revoke("b") is b
    assert table.active() == []
    table.acquire("c", 0)
    assert [lease.robot_id for lease in table.expire(1_000)] == ["c"]


def test_generations_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "leases.db"
    store = SqliteGenerationStore(path)
    first = LeaseTable(store, ttl_us=10).acquire("r1", 0)
    store.close()
    reopened = SqliteGenerationStore(path)
    second = LeaseTable(reopened, ttl_us=10).acquire("r1", 0)
    reopened.close()
    assert isinstance(first, Lease)
    assert isinstance(second, Lease)
    assert second.generation == first.generation + 1


def test_lease_ttl_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        LeaseTable(MemoryGenerationStore(), ttl_us=0)


# --- profiles ----------------------------------------------------------------------------------


def _profile() -> LatencyProfile:
    return LatencyProfile(
        [ProfilePoint(1, 10 * MS, 12 * MS, 500), ProfilePoint(4, 16 * MS, 20 * MS, 800)]
    )


def test_profile_interpolates_and_extrapolates_conservatively() -> None:
    profile = _profile()
    assert profile.p99_us(1) == 12 * MS
    assert profile.p99_us(2) == pytest.approx(14_666, abs=2)
    assert profile.p99_us(8) > profile.p99_us(4)
    assert profile.memory_mib(4) == 800


def test_profile_adapts_upward_only() -> None:
    profile = _profile()
    profile.observe(1, 5 * MS)
    profile.observe(1, 5 * MS)
    assert profile.p99_us(1) == 12 * MS  # faster observations never make predictions optimistic
    for _ in range(20):
        profile.observe(1, 30 * MS)
    assert profile.p99_us(1) == 30 * MS


def test_one_stall_does_not_raise_the_profile_but_a_repeated_slowdown_does() -> None:
    profile = _profile()
    for _ in range(50):
        profile.observe(1, 10 * MS, now_us=0)
    profile.observe(1, 3_000 * MS, now_us=0)  # a host paused while this batch ran
    assert profile.p99_us(1) == 12 * MS
    profile.observe(1, 2_000 * MS, now_us=0)
    assert profile.p99_us(1) == 2_000 * MS


def test_observations_expire_so_an_inflated_profile_recovers() -> None:
    profile = LatencyProfile([ProfilePoint(1, 10 * MS, 12 * MS)], horizon_us=60 * 1_000_000)
    for at in (0, 1, 2):
        profile.observe(1, 900 * MS, now_us=at)
    assert profile.p99_us(1) == 900 * MS
    profile.expire(now_us=59 * 1_000_000)
    assert profile.p99_us(1) == 900 * MS
    profile.expire(now_us=61 * 1_000_000)
    assert profile.p99_us(1) == 12 * MS  # without traffic, the prior is back after the horizon


@pytest.mark.parametrize(
    "points",
    [
        [],
        [ProfilePoint(0, 1, 1)],
        [ProfilePoint(1, 10, 5)],
        [ProfilePoint(1, 10, 10), ProfilePoint(1, 10, 10)],
    ],
)
def test_profile_rejects_bad_points(points: list[ProfilePoint]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        LatencyProfile(points)


# --- admission ---------------------------------------------------------------------------------


def _demand(
    i: int, budget_ms: int = 600, interval_ms: int = 1_000, link_ms: int = 2
) -> SessionDemand:
    return SessionDemand(f"s{i}", budget_ms * MS, interval_ms * MS, link_ms * MS)


def test_admits_until_burst_would_miss_budgets() -> None:
    admission = AdmissionController(_profile(), max_batch=4, reserve_fraction=0.0)
    for i in range(40):
        decision = admission.try_admit(_demand(i, budget_ms=100))
        if not decision.admitted:
            assert decision.reason is ReasonCode.ADMISSION_CAPACITY_EXCEEDED
            break
    admitted = len(admission.admitted)
    # Burst model: one running batch blocks (20 ms at batch 4), then batches of up to 4 in
    # deadline order, plus 2 ms link. 15 sessions: 20 + 3 * 20 + p99(3) = 17.3, + 2 = 99.3 ms.
    # A 16th makes it 20 + 4 * 20 + 2 = 102 ms > 100 ms.
    assert admitted == 15


def test_utilization_reserve_is_enforced() -> None:
    admission = AdmissionController(_profile(), max_batch=4, reserve_fraction=0.5)
    decisions = [
        admission.try_admit(_demand(i, budget_ms=10_000, interval_ms=60)) for i in range(20)
    ]
    rejected = [d for d in decisions if not d.admitted]
    assert rejected
    assert "utilization" in rejected[0].detail


def test_memory_headroom_is_enforced() -> None:
    admission = AdmissionController(_profile(), max_batch=4, memory_available_mib=550)
    assert admission.try_admit(_demand(0)).admitted
    decision = admission.try_admit(_demand(1))
    assert not decision.admitted
    assert decision.reason is ReasonCode.ADMISSION_MEMORY_EXCEEDED


def test_removal_frees_capacity_and_estimate_matches() -> None:
    admission = AdmissionController(_profile(), max_batch=4, reserve_fraction=0.0)
    estimate = admission.capacity_estimate(_demand(0, budget_ms=100))
    for i in range(estimate):
        assert admission.try_admit(_demand(i, budget_ms=100)).admitted
    assert not admission.try_admit(_demand(99, budget_ms=100)).admitted
    admission.remove("s0")
    assert admission.try_admit(_demand(99, budget_ms=100)).admitted


def _fresh_demand(session_id: str) -> SessionDemand:
    # The CPU profile's limits shape: an observation at most 100 ms old when it reaches the model.
    return SessionDemand(session_id, 600 * MS, 1_000 * MS, 2 * MS, 100 * MS, 1_500 * MS)


def _slow_profile() -> LatencyProfile:
    # A pass slower than the dispatch-freshness limit, as on a CPU.
    return LatencyProfile(
        [ProfilePoint(1, 140 * MS, 150 * MS), ProfilePoint(4, 200 * MS, 220 * MS)]
    )


def test_a_lone_robot_never_waits_behind_its_own_request() -> None:
    # A robot has at most one request outstanding, so with no other robot nothing is running
    # when it asks, however slow a pass is.
    admission = AdmissionController(_slow_profile(), max_batch=4, batch_window_us=2 * MS)
    decision = admission.try_admit(_fresh_demand("s0"))
    assert decision.admitted, decision.detail
    assert decision.worst_case_completion_us == (2 + 150 + 2) * MS  # window, pass, link


def test_robots_still_wait_behind_each_others_batch() -> None:
    admission = AdmissionController(_slow_profile(), max_batch=4, batch_window_us=2 * MS)
    assert admission.try_admit(_fresh_demand("s0")).admitted
    decision = admission.try_admit(_fresh_demand("s1"))
    assert not decision.admitted
    assert decision.limit == "dispatch_freshness"
    # Behind the other robot's 150 ms pass and the window, plus half the link.
    assert decision.detail.startswith("2 sessions: observation age at dispatch 153000 us")


def test_a_robot_reconnecting_after_a_stall_gets_the_same_verdict() -> None:
    # The stall outlasted the lease, so the gateway dropped the session and the robot opened a
    # new one. By then the profile had learned this host's slower passes.
    profile = _profile()
    admission = AdmissionController(profile, max_batch=4, batch_window_us=2 * MS)
    assert admission.try_admit(_fresh_demand("s0")).admitted
    for _ in range(20):
        profile.observe(1, 230 * MS)
    admission.remove("s0")
    decision = admission.try_admit(_fresh_demand("s1"))
    assert decision.admitted, decision.detail


def test_demand_from_binding(make_binding: Any) -> None:
    binding = make_binding()
    demand = SessionDemand.from_binding("s", binding, rtt_p99_us=3_000)
    period = binding.control_period_us
    assert demand.budget_us == 20 * period - period
    assert demand.interval_us == 30 * period
    assert demand.link_us == 6_000


def test_a_profile_never_predicts_a_larger_batch_faster() -> None:
    # Measured p99 at batch 2 caught a tail sample that batch 4's window did not.
    profile = LatencyProfile(
        [
            ProfilePoint(1, 70_000, 90_000, 800),
            ProfilePoint(2, 100_000, 190_000, 830),
            ProfilePoint(4, 115_000, 150_000, 820),
        ]
    )
    assert profile.p99_us(4) == 190_000
    assert profile.p50_us(4) == 115_000
    assert profile.memory_mib(4) == 830
    assert profile.p99_us(3) == 190_000
