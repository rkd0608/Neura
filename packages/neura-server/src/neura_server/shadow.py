"""Shadow serving (design §28.3): a candidate model on the observations robots send the model they
run, its actions compared with the running model's and never sent to a robot.

Sans-IO, like ``GatewayCore``: the gateway offers each accepted observation and each delivered
chunk; a dispatcher of its own runs the candidate. The running model always comes first:
- the candidate runs in a batch group (and worker) of its own, never in a robot's scheduler;
- it has at most one request in flight per shadow: an observation offered while it is busy is
  skipped and counted, never queued;
- its results have no deadline, since no robot waits for them;
- its samples are spread over the robots it shadows: robots served in one batch send in step, and
  the first of them would otherwise take every sample.

Both chunks are compared in robot command units, each through its own execution profile, over
the actions a robot executes between refills, as a share of each command's declared range. The
comparison says how far the candidate's actions are from what the running model did on the same
live observation; it cannot say which is better.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_contracts import DeviceKind
from neura_runtime.binding import ExecutionBinding
from neura_runtime.policy import ModelInputs

# Results kept for percentiles: enough for a stable p95, bounded however long a shadow runs.
WINDOW = 10_000
# A primary result that has not arrived after this many seconds is not coming (dropped,
# superseded, expired): its candidate result is discarded.
PAIR_TIMEOUT_US = 10_000_000
# A robot whose observations reached the shadow this recently is one it is sampling now.
ACTIVE_US = 2_000_000


@dataclass(frozen=True, slots=True)
class ShadowDeployment:
    revision_id: str  # the candidate
    of_revision_id: str  # the revision whose robots it shadows
    binding: ExecutionBinding
    robots: frozenset[str]
    device: str | None = None
    every_nth: int = 1

    @property
    def device_name(self) -> str:
        if self.device is not None:
            return self.device
        return "cuda:0" if self.binding.profile.spec.runtime.device is DeviceKind.CUDA else "cpu"

    @property
    def group_key(self) -> str:
        # Never the key of a serving group, even for the same artifact: a shadow never shares a
        # scheduler or a worker with robots.
        return f"shadow:{self.revision_id}:{self.binding.artifact_digest}"


@dataclass(slots=True)
class ShadowRequest:
    key: tuple[str, int]  # (session, observation sequence)
    robot_id: str
    inputs: ModelInputs
    offered_at_us: int


@dataclass(slots=True)
class _Pair:
    offered_at_us: int
    robot_id: str
    primary: NDArray[np.float64] | None = None  # robot units
    candidate: NDArray[np.float64] | None = None
    horizon: int | None = None  # the running revision's executed actions


def executed_horizon(binding: ExecutionBinding) -> int:
    """The actions a robot executes from a chunk before the next one replaces it."""
    chunk = binding.profile.spec.chunk
    return max(1, chunk.actions_per_chunk - chunk.refill_threshold_actions)


def comparable(candidate: ExecutionBinding, running: ExecutionBinding) -> str | None:
    """Why a candidate cannot shadow a running revision, or None."""
    if candidate.robot_profile_digest != running.robot_profile_digest:
        return "the candidate is for another robot profile"
    if candidate.control_period_us != running.control_period_us:
        return "the candidate's actions are for another control period"
    return None


@dataclass(slots=True)
class ShadowStats:
    offered: int = 0
    compared: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    invalid_outputs: int = 0
    failed: int = 0
    out_of_range: Counter[str] = field(default_factory=Counter)  # "candidate", "running"
    total: float = 0.0
    maximum: float = 0.0
    recent: deque[float] = field(default_factory=lambda: deque(maxlen=WINDOW))
    by_robot: Counter[str] = field(default_factory=Counter)

    def add(self, robot_id: str, divergence: float) -> None:
        self.compared += 1
        self.total += divergence
        self.maximum = max(self.maximum, divergence)
        self.recent.append(divergence)
        self.by_robot[robot_id] += 1

    def to_json(self) -> dict[str, Any]:
        divergence: dict[str, float] = {}
        if self.compared:
            divergence = {
                "mean": round(self.total / self.compared, 6),
                "p95": round(float(np.percentile(np.array(self.recent), 95)), 6),
                "max": round(self.maximum, 6),
            }
        return {
            "offered": self.offered,
            "compared": self.compared,
            "robots": len(self.by_robot),
            "skipped": dict(self.skipped),
            "invalid_outputs": self.invalid_outputs,
            "failed": self.failed,
            "out_of_range": dict(self.out_of_range),
            "divergence": divergence,
        }


class ShadowTracker:
    """One shadow's sampling, pairing and comparison."""

    def __init__(self, shadow: ShadowDeployment) -> None:
        self.shadow = shadow
        self.stats = ShadowStats()
        self.pending: ShadowRequest | None = None
        self.in_flight: ShadowRequest | None = None
        self._pairs: dict[tuple[str, int], _Pair] = {}
        self._seen: Counter[str] = Counter()
        self._taken: Counter[str] = Counter()  # by robot
        self._last_offer_us: dict[str, int] = {}  # by robot, its last usable observation

    @property
    def busy(self) -> bool:
        return self.pending is not None or self.in_flight is not None

    def offer(
        self,
        session_id: str,
        robot_id: str,
        sequence: int,
        inputs: ModelInputs | None,
        now_us: int,
        *,
        invalid: bool = False,
    ) -> bool:
        """An observation the running model accepted. Returns True when the candidate takes it."""
        if robot_id not in self.shadow.robots:
            return False
        self._seen[session_id] += 1
        if (self._seen[session_id] - 1) % self.shadow.every_nth:
            return False
        self.stats.offered += 1
        if invalid or inputs is None:
            self.stats.skipped["invalid_input"] += 1
            return False
        self._last_offer_us[robot_id] = now_us
        if self.busy:
            self.stats.skipped["busy"] += 1
            return False
        if self._ahead(robot_id, now_us):
            self.stats.skipped["balance"] += 1  # left free for a robot with fewer samples
            return False
        self._taken[robot_id] += 1
        key = (session_id, sequence)
        self.pending = ShadowRequest(key, robot_id, inputs, now_us)
        self._pairs[key] = _Pair(now_us, robot_id)
        return True

    def _ahead(self, robot_id: str, now_us: int) -> bool:
        """Whether another robot sending now has had fewer of its observations taken."""
        taken = self._taken[robot_id]
        return any(
            self._taken[other] < taken
            for other, at in self._last_offer_us.items()
            if other != robot_id and now_us - at <= ACTIVE_US
        )

    def take(self) -> ShadowRequest | None:
        """The request for the candidate to run now, if any."""
        request, self.pending = self.pending, None
        self.in_flight = request
        return request

    def primary(
        self, session_id: str, sequence: int, output: NDArray[np.float32], binding: ExecutionBinding
    ) -> None:
        """The running model's chunk for an observation, as delivered to its robot."""
        pair = self._pairs.get((session_id, sequence))
        if pair is None:
            return
        pair.primary = self._robot_units(output, binding, "running")
        pair.horizon = executed_horizon(binding)
        self._compare((session_id, sequence), pair)

    def skip(self, request: ShadowRequest, why: str) -> None:
        """The candidate could not be asked (its worker is starting or restarting): no attempt,
        so no failure."""
        if self.in_flight is request:
            self.in_flight = None
        self._pairs.pop(request.key, None)
        self.stats.skipped[why] += 1

    def candidate(self, request: ShadowRequest, output: NDArray[np.float32] | None) -> None:
        """The candidate's chunk (None: its worker failed)."""
        if self.in_flight is request:
            self.in_flight = None
        pair = self._pairs.get(request.key)
        if output is None:
            self.stats.failed += 1
            self._pairs.pop(request.key, None)
            return
        binding = self.shadow.binding
        expected = (binding.chunk_length, binding.action_dim)
        if output.shape != expected or not np.isfinite(output).all():
            self.stats.invalid_outputs += 1
            self._pairs.pop(request.key, None)
            return
        if pair is None:
            return
        pair.candidate = self._robot_units(output, binding, "candidate")
        self._compare(request.key, pair)

    def expire(self, now_us: int) -> None:
        """Forget observations whose running-model result never came (dropped, superseded or
        expired at the gateway). One the candidate is still working on is kept."""
        running = self.in_flight.key if self.in_flight is not None else None
        for key, pair in list(self._pairs.items()):
            if key != running and now_us - pair.offered_at_us > PAIR_TIMEOUT_US:
                del self._pairs[key]
                self.stats.skipped["unpaired"] += 1

    def _robot_units(
        self, output: NDArray[np.float32], binding: ExecutionBinding, role: str
    ) -> NDArray[np.float64]:
        robot = binding.action_transform.to_robot(output.astype(np.float64))
        if binding.action_transform.out_of_range(robot):
            self.stats.out_of_range[role] += 1
        return robot

    def _compare(self, key: tuple[str, int], pair: _Pair) -> None:
        if pair.primary is None or pair.candidate is None:
            return
        del self._pairs[key]
        transform = self.shadow.binding.action_transform
        span = np.where(transform.high > transform.low, transform.high - transform.low, 1.0)
        horizon = min(len(pair.primary), len(pair.candidate), pair.horizon or len(pair.primary))
        diff = np.abs(pair.candidate[:horizon] - pair.primary[:horizon]) / span
        value = float(diff.mean())
        if math.isfinite(value):
            self.stats.add(pair.robot_id, value)


def shadow_verdict(stats: dict[str, Any], *, min_samples: int, tolerance: float) -> tuple[str, str]:
    """(verdict, why). ``non_inferior`` here means the candidate stayed within ``tolerance`` of
    the running model's actions on live observations; ``inferior`` that it did not, or that it
    produced actions no robot could use."""
    if stats.get("invalid_outputs", 0) or stats.get("failed", 0):
        return "inferior", "the candidate failed or produced invalid actions on live observations"
    if stats.get("out_of_range", {}).get("candidate", 0) > stats.get("out_of_range", {}).get(
        "running", 0
    ):
        return "inferior", "the candidate asked for commands outside the robot's ranges"
    compared = int(stats.get("compared", 0))
    if compared < min_samples:
        return "inconclusive", f"{compared} observations compared; {min_samples} are needed"
    mean = float(stats.get("divergence", {}).get("mean", math.inf))
    if mean <= tolerance:
        return "non_inferior", f"mean divergence {mean:.4f} is within {tolerance}"
    return "inferior", f"mean divergence {mean:.4f} exceeds {tolerance}"


@dataclass(frozen=True, slots=True)
class ShadowSuite:
    """How long a candidate shadows, and what it must show."""

    duration_s: float = 600.0
    # Fewer observations compared than this: inconclusive.
    min_samples: int = 200
    # Stop early once this many are compared; None: run the whole duration.
    max_samples: int | None = None
    every_nth: int = 1
    # The largest mean divergence still called close to the running model, as a share of each
    # command's range.
    tolerance: float = 0.05

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ShadowSuite:
        suite = cls(**{k: v for k, v in data.items() if k != "kind"})
        if not 1 <= suite.duration_s <= 7 * 86_400:
            raise ValueError("duration_s is between 1 second and 7 days")
        if not 1 <= suite.min_samples <= 10_000_000:
            raise ValueError("min_samples must be positive")
        if suite.max_samples is not None and suite.max_samples < suite.min_samples:
            raise ValueError("max_samples must be at least min_samples")
        if not 1 <= suite.every_nth <= 10_000:
            raise ValueError("every_nth is between 1 and 10000")
        if not 0 < suite.tolerance <= 1:
            raise ValueError("tolerance is a share of each command's range, above 0 and at most 1")
        return suite

    def to_json(self) -> dict[str, Any]:
        return {"kind": "shadow", **{k: getattr(self, k) for k in self.__dataclass_fields__}}
