"""The conditions both loops are run under (``compare.loops``): how long the policy takes to answer
each request, and what goes wrong when.

Every condition is a function of the time a request is made, so both loops meet exactly the same
policy: the same answer times, the same crash, the same network drop, the same takeover. Times are
microseconds on the run's clock, from the start of the task.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

S = 1_000_000  # microseconds in a second


@dataclass(frozen=True)
class Condition:
    name: str
    title: str
    what: str  # one line for the report
    latency_us: int  # the policy's typical answer time
    jitter_us: int = 0  # +- uniformly
    spikes: tuple[int, ...] = ()  # answer times drawn instead, now and then
    spike_every: int = 0  # one request in this many is a spike (0: never)
    crash_at_us: int | None = None  # the policy fails every request from then on
    drop: tuple[int, int] | None = None  # the network is down from..to: answers wait for it
    takeover_at_us: int | None = None  # the operator takes over
    measured: tuple[int, ...] = field(default=())  # answer times measured on a real model

    def answer_time(self, request: int, sent_us: int) -> int | None:
        """How long request number ``request`` (sent at ``sent_us``) takes to come back; None if
        the policy fails it."""
        if self.crash_at_us is not None and sent_us >= self.crash_at_us:
            return None
        rng = random.Random(request * 7919 + 17)  # noqa: S311  # the same for both loops
        if self.measured:
            latency = self.measured[request % len(self.measured)]
        elif self.spike_every and request % self.spike_every == self.spike_every - 1:
            latency = rng.choice(self.spikes)
        else:
            latency = self.latency_us + rng.randint(-self.jitter_us, self.jitter_us)
        if self.drop is not None and self.drop[0] <= sent_us < self.drop[1]:
            latency = max(latency, self.drop[1] - sent_us + self.latency_us)
        return latency


GPU_US, GPU_JITTER_US = 100_000, 20_000  # a GPU server down the hall: about 100 ms

CONDITIONS = [
    Condition(
        "steady",
        "Steady GPU server",
        "answers in 80-120 ms, nothing goes wrong",
        latency_us=GPU_US,
        jitter_us=GPU_JITTER_US,
    ),
    Condition(
        "spikes",
        "Latency spikes",
        "1 answer in 8 takes 0.5-0.9 s (a busy GPU, garbage collection, a congested link)",
        spikes=(500_000, 700_000, 900_000),
        spike_every=8,
        latency_us=GPU_US,
        jitter_us=GPU_JITTER_US,
    ),
    Condition(
        "drop",
        "Network drop",
        "the link is down for 1 s at t = 5 s",
        drop=(5 * S, 6 * S),
        latency_us=GPU_US,
        jitter_us=GPU_JITTER_US,
    ),
    Condition(
        "crash",
        "Policy crash",
        "the policy fails every request from t = 5 s",
        crash_at_us=5 * S,
        latency_us=GPU_US,
        jitter_us=GPU_JITTER_US,
    ),
    Condition(
        "takeover",
        "Operator takeover",
        "the operator takes over at t = 5 s, while the arm is moving",
        takeover_at_us=5 * S,
        latency_us=GPU_US,
        jitter_us=GPU_JITTER_US,
    ),
]


def measured_condition(latencies_us: list[int], where: str) -> Condition:
    """The answer times of a real model, measured (``compare.smolvla``)."""
    median = sorted(latencies_us)[len(latencies_us) // 2]
    return Condition(
        "smolvla-cpu",
        "SmolVLA on a CPU (measured)",
        f"SmolVLA's real answer times on {where}: median {median / S:.2f} s",
        latency_us=median,
        measured=tuple(latencies_us),
    )
