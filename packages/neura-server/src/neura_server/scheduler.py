"""Per-batch-group request scheduling (design §9.2).

One ``Scheduler`` serves one batch group (one artifact on one device at one precision): only
requests in the same group ever share a forward pass. The scheduler is work-conserving: it never
waits to fill a batch. All times are on the gateway's clock.
"""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from neura.v1 import common_pb2
from neura_adapters import ModelInputs
from neura_contracts import ReasonCode
from neura_server.profiles import LatencyProfile


class Policy(StrEnum):
    FIFO = "fifo"
    ROUND_ROBIN = "round_robin"
    DEADLINE_FAIRNESS = "deadline_fairness"


@dataclass(slots=True)
class InferenceRequest:
    session_id: str
    envelope: common_pb2.Envelope
    anchor_index: int
    inputs: ModelInputs
    received_at_us: int
    deadline_us: int  # when the robot's queue is predicted to starve
    dispatch_by_us: int  # after this the observation is too old to dispatch
    arrival: int = 0
    skips: int = 0

    @property
    def task_epoch(self) -> int:
        return self.envelope.task_epoch

    @property
    def sequence(self) -> int:
        return self.envelope.sequence


@dataclass(frozen=True, slots=True)
class Dropped:
    request: InferenceRequest
    reason: ReasonCode


@dataclass(frozen=True, slots=True)
class Batch:
    batch_id: int
    requests: tuple[InferenceRequest, ...]
    dispatched_at_us: int
    predicted_completion_us: int


@dataclass(slots=True)
class SchedulerStats:
    submitted: int = 0
    dispatched_requests: int = 0
    batches: int = 0
    batch_sizes: Counter[int] = field(default_factory=Counter)
    # Completed after the predicted starvation time, for requests that arrived with budget left.
    deadline_misses: int = 0
    # Requests from robots that were already starved (arming, or waiting) when they asked.
    starved_on_arrival: int = 0
    dropped: Counter[str] = field(default_factory=Counter)


class Scheduler:
    def __init__(
        self,
        profile: LatencyProfile,
        *,
        policy: Policy = Policy.DEADLINE_FAIRNESS,
        max_batch: int = 8,
        max_skips: int = 3,
    ) -> None:
        if max_batch < 1:
            raise ValueError("max_batch must be >= 1")
        self.profile = profile
        self.policy = policy
        self.max_batch = max_batch
        self.max_skips = max_skips
        self._queued: dict[str, InferenceRequest] = {}  # at most one queued request per session
        self._in_flight: dict[str, InferenceRequest] = {}
        self._session_epoch: dict[str, int] = {}
        self._last_served: dict[str, int] = {}
        self._arrivals = itertools.count()
        self._batch_ids = itertools.count(1)
        self._dispatch_counter = itertools.count(1)
        self.stats = SchedulerStats()

    # --- intake -------------------------------------------------------------------------------

    def submit(self, request: InferenceRequest) -> list[Dropped]:
        """Queue a request. Returns requests dropped as a consequence (including this one)."""
        dropped: list[Dropped] = []
        known = self._session_epoch.get(request.session_id, 0)
        if request.task_epoch < known:
            dropped.append(Dropped(request, ReasonCode.REQUEST_FENCED))
            self._count(dropped)
            return dropped
        self._session_epoch[request.session_id] = request.task_epoch
        previous = self._queued.pop(request.session_id, None)
        if previous is not None:
            dropped.append(Dropped(previous, ReasonCode.REQUEST_SUPERSEDED))
        request.arrival = next(self._arrivals)
        self._queued[request.session_id] = request
        self.stats.submitted += 1
        self._count(dropped)
        return dropped

    def advance_epoch(self, session_id: str, epoch: int) -> list[Dropped]:
        """The robot announced a new task epoch: cancel queued work for older epochs."""
        self._session_epoch[session_id] = max(epoch, self._session_epoch.get(session_id, 0))
        dropped: list[Dropped] = []
        queued = self._queued.get(session_id)
        if queued is not None and queued.task_epoch < epoch:
            del self._queued[session_id]
            dropped.append(Dropped(queued, ReasonCode.REQUEST_FENCED))
        self._count(dropped)
        return dropped

    def cancel_session(self, session_id: str, reason: ReasonCode) -> list[Dropped]:
        dropped: list[Dropped] = []
        queued = self._queued.pop(session_id, None)
        if queued is not None:
            dropped.append(Dropped(queued, reason))
        self._in_flight.pop(session_id, None)
        self._session_epoch.pop(session_id, None)
        self._last_served.pop(session_id, None)
        self._count(dropped)
        return dropped

    # --- dispatch -----------------------------------------------------------------------------

    def has_work(self) -> bool:
        return bool(self._queued)

    def expire(self, now_us: int) -> list[Dropped]:
        expired = [r for r in self._queued.values() if r.dispatch_by_us < now_us]
        for request in expired:
            del self._queued[request.session_id]
        dropped = [Dropped(r, ReasonCode.REQUEST_EXPIRED) for r in expired]
        self._count(dropped)
        return dropped

    def next_batch(self, now_us: int) -> tuple[Batch | None, list[Dropped]]:
        dropped = self.expire(now_us)
        if not self._queued:
            return None, dropped
        ordered = self._order(self._queued.values())
        head = ordered[0]
        members = [head]
        for candidate in ordered[1:]:
            if len(members) >= self.max_batch:
                break
            trial = [*members, candidate]
            completion = now_us + self.profile.p99_us(len(trial))
            # Late members do not constrain batching: delaying them further costs less than
            # leaving the GPU underused while others wait. Feasible members must stay feasible.
            feasible = [
                r.deadline_us for r in trial if r.deadline_us >= now_us + self.profile.p99_us(1)
            ]
            if feasible and completion > min(feasible):
                continue
            members = trial
        chosen = {r.session_id for r in members}
        for request in self._queued.values():
            if request.session_id not in chosen:
                request.skips += 1
        dispatch_number = next(self._dispatch_counter)
        for request in members:
            del self._queued[request.session_id]
            self._in_flight[request.session_id] = request
            self._last_served[request.session_id] = dispatch_number
        batch = Batch(
            batch_id=next(self._batch_ids),
            requests=tuple(members),
            dispatched_at_us=now_us,
            predicted_completion_us=now_us + self.profile.p99_us(len(members)),
        )
        self.stats.batches += 1
        self.stats.dispatched_requests += len(members)
        self.stats.batch_sizes[len(members)] += 1
        return batch, dropped

    def complete(self, batch: Batch, now_us: int) -> list[InferenceRequest]:
        """Record a finished batch. Returns the requests whose results are still deliverable."""
        self.profile.observe(len(batch.requests), now_us - batch.dispatched_at_us)
        deliverable = []
        for request in batch.requests:
            if self._in_flight.get(request.session_id) is request:
                del self._in_flight[request.session_id]
                if request.deadline_us <= request.received_at_us:
                    self.stats.starved_on_arrival += 1
                elif now_us > request.deadline_us:
                    self.stats.deadline_misses += 1
                if request.task_epoch >= self._session_epoch.get(request.session_id, 0):
                    deliverable.append(request)
                else:
                    self.stats.dropped[ReasonCode.REQUEST_FENCED.value] += 1
        return deliverable

    def fail(self, batch: Batch) -> list[InferenceRequest]:
        """A batch failed (worker crash, OOM). Returns the requests that were still in flight."""
        failed = []
        for request in batch.requests:
            if self._in_flight.get(request.session_id) is request:
                del self._in_flight[request.session_id]
                failed.append(request)
        return failed

    def _order(self, requests: Iterable[InferenceRequest]) -> list[InferenceRequest]:
        items = list(requests)
        if self.policy is Policy.FIFO:
            return sorted(items, key=lambda r: r.arrival)
        if self.policy is Policy.ROUND_ROBIN:
            return sorted(items, key=lambda r: (self._last_served.get(r.session_id, 0), r.arrival))
        # Deadline first, but a request skipped max_skips times jumps ahead (fairness).
        return sorted(
            items,
            key=lambda r: (0 if r.skips >= self.max_skips else 1, r.deadline_us, r.arrival),
        )

    def _count(self, dropped: list[Dropped]) -> None:
        for item in dropped:
            self.stats.dropped[item.reason.value] += 1

    @property
    def queued(self) -> int:
        return len(self._queued)

    @property
    def in_flight(self) -> int:
        return len(self._in_flight)
