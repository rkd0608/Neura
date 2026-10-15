from __future__ import annotations

import numpy as np
import pytest

from neura.v1 import common_pb2
from neura_adapters import ModelInputs
from neura_contracts import ReasonCode
from neura_server.profiles import LatencyProfile, ProfilePoint
from neura_server.scheduler import InferenceRequest, Policy, Scheduler

MS = 1_000
INPUTS = ModelInputs(state={}, images={}, instruction="", frozen_prefix=None)


def profile() -> LatencyProfile:
    return LatencyProfile(
        [
            ProfilePoint(1, 10 * MS, 10 * MS),
            ProfilePoint(2, 12 * MS, 12 * MS),
            ProfilePoint(8, 30 * MS, 30 * MS),
        ]
    )


def request(
    session: str,
    *,
    deadline_ms: int = 1_000,
    received_ms: int = 0,
    dispatch_by_ms: int = 10_000,
    epoch: int = 1,
    sequence: int = 1,
) -> InferenceRequest:
    return InferenceRequest(
        session_id=session,
        envelope=common_pb2.Envelope(session_id=session, task_epoch=epoch, sequence=sequence),
        anchor_index=0,
        inputs=INPUTS,
        received_at_us=received_ms * MS,
        deadline_us=deadline_ms * MS,
        dispatch_by_us=dispatch_by_ms * MS,
    )


def sessions(batch: object) -> list[str]:
    assert batch is not None
    return [r.session_id for r in batch.requests]  # type: ignore[attr-defined]


def test_batches_compatible_requests_up_to_max() -> None:
    s = Scheduler(profile(), max_batch=3)
    for name in "abcd":
        s.submit(request(name))
    batch, _ = s.next_batch(0)
    assert sessions(batch) == ["a", "b", "c"]
    batch2, _ = s.next_batch(0)
    assert sessions(batch2) == ["d"]


def test_never_extends_batch_past_a_feasible_deadline() -> None:
    s = Scheduler(profile(), max_batch=8)
    s.submit(request("tight", deadline_ms=11))  # p99(1)=10 ms fits; p99(2)=12 ms does not
    s.submit(request("loose", deadline_ms=1_000))
    batch, _ = s.next_batch(0)
    assert sessions(batch) == ["tight"]


def test_late_requests_do_not_block_batching() -> None:
    s = Scheduler(profile(), max_batch=8)
    s.submit(request("late", deadline_ms=1))
    s.submit(request("other", deadline_ms=1_000))
    batch, _ = s.next_batch(0)
    assert sorted(sessions(batch)) == ["late", "other"]


def test_edf_orders_by_deadline_fifo_by_arrival() -> None:
    edf = Scheduler(profile(), max_batch=1, policy=Policy.DEADLINE_FAIRNESS)
    fifo = Scheduler(profile(), max_batch=1, policy=Policy.FIFO)
    for sched in (edf, fifo):
        sched.submit(request("late-deadline", deadline_ms=900))
        sched.submit(request("early-deadline", deadline_ms=100))
    assert sessions(edf.next_batch(0)[0]) == ["early-deadline"]
    assert sessions(fifo.next_batch(0)[0]) == ["late-deadline"]


def test_round_robin_serves_least_recently_served_first() -> None:
    s = Scheduler(profile(), max_batch=1, policy=Policy.ROUND_ROBIN)
    s.submit(request("a"))
    s.submit(request("b"))
    assert sessions(s.next_batch(0)[0]) == ["a"]
    s.submit(request("a", sequence=2))
    assert sessions(s.next_batch(0)[0]) == ["b"]


def test_fairness_promotes_repeatedly_skipped_requests() -> None:
    s = Scheduler(profile(), max_batch=1, max_skips=2)
    s.submit(request("patient", deadline_ms=5_000))
    for i in range(2):
        s.submit(request(f"urgent{i}", deadline_ms=10 + i))
        assert sessions(s.next_batch(0)[0]) == [f"urgent{i}"]
    s.submit(request("urgent9", deadline_ms=5))
    assert sessions(s.next_batch(0)[0]) == ["patient"]


def test_newer_request_supersedes_queued_one() -> None:
    s = Scheduler(profile())
    s.submit(request("a", sequence=1))
    dropped = s.submit(request("a", sequence=2))
    assert [(d.request.sequence, d.reason) for d in dropped] == [(1, ReasonCode.REQUEST_SUPERSEDED)]
    batch, _ = s.next_batch(0)
    assert batch is not None
    assert batch.requests[0].sequence == 2


def test_old_epochs_are_fenced() -> None:
    s = Scheduler(profile())
    s.submit(request("a", epoch=3))
    s.next_batch(0)
    dropped = s.submit(request("a", epoch=2))
    assert dropped[0].reason is ReasonCode.REQUEST_FENCED
    s.submit(request("b", epoch=1))
    fenced = s.advance_epoch("b", 2)
    assert fenced[0].reason is ReasonCode.REQUEST_FENCED
    assert not s.has_work()


def test_expired_requests_are_dropped_before_dispatch() -> None:
    s = Scheduler(profile())
    s.submit(request("a", dispatch_by_ms=5))
    batch, dropped = s.next_batch(6 * MS)
    assert batch is None
    assert dropped[0].reason is ReasonCode.REQUEST_EXPIRED


def test_completion_delivers_only_current_work_and_counts_misses() -> None:
    s = Scheduler(profile())
    s.submit(request("a", deadline_ms=5, received_ms=0))
    s.submit(request("b", deadline_ms=0, received_ms=0))  # starved on arrival
    batch, _ = s.next_batch(0)
    assert batch is not None
    s.advance_epoch("a", 1)  # same epoch: still deliverable
    deliverable = s.complete(batch, 20 * MS)
    assert sorted(r.session_id for r in deliverable) == ["a", "b"]
    assert s.stats.deadline_misses == 1
    assert s.stats.starved_on_arrival == 1


def test_results_for_fenced_or_cancelled_sessions_are_not_delivered() -> None:
    s = Scheduler(profile())
    s.submit(request("a", epoch=1))
    s.submit(request("b", epoch=1))
    batch, _ = s.next_batch(0)
    assert batch is not None
    s.advance_epoch("a", 2)
    s.cancel_session("b", ReasonCode.LEASE_EXPIRED)
    assert s.complete(batch, 10 * MS) == []


def test_failed_batch_returns_in_flight_requests() -> None:
    s = Scheduler(profile())
    s.submit(request("a"))
    batch, _ = s.next_batch(0)
    assert batch is not None
    assert [r.session_id for r in s.fail(batch)] == ["a"]
    assert s.fail(batch) == []


def test_profile_observes_completion_latency() -> None:
    p = profile()
    s = Scheduler(p)
    for _ in range(10):
        s.submit(request("a"))
        batch, _ = s.next_batch(0)
        assert batch is not None
        s.complete(batch, 50 * MS)
    assert p.p99_us(1) == 50 * MS


def test_max_batch_validation() -> None:
    with pytest.raises(ValueError, match="max_batch"):
        Scheduler(profile(), max_batch=0)


def test_inputs_are_passed_through() -> None:
    s = Scheduler(profile())
    inputs = ModelInputs(
        state={"x": np.zeros(2, dtype=np.float32)}, images={}, instruction="i", frozen_prefix=None
    )
    r = request("a")
    r.inputs = inputs
    s.submit(r)
    batch, _ = s.next_batch(0)
    assert batch is not None
    assert batch.requests[0].inputs is inputs
