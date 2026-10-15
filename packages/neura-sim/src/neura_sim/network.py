"""A one-way network link model for the discrete-event simulator.

Latency is ``base + uniform jitter`` plus any active delay spike. Like a TCP stream, a link
delivers in order unless reordering is injected. Partitions drop everything sent while active;
loss windows drop each message with a probability.
Duplication and reordering model replays and misbehaving middleboxes, which the runtime must
tolerate even though gRPC itself would not produce them.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass(slots=True)
class Window:
    start_us: int
    end_us: int

    def contains(self, t_us: int) -> bool:
        return self.start_us <= t_us < self.end_us


@dataclass(slots=True)
class LinkModel:
    base_us: int = 500
    jitter_us: int = 200
    partitions: list[Window] = field(default_factory=list)
    spikes: list[tuple[Window, int]] = field(default_factory=list)  # window, extra delay
    losses: list[tuple[Window, float]] = field(default_factory=list)  # window, drop probability
    duplicate_probability: float = 0.0
    reorder_probability: float = 0.0
    reorder_extra_us: int = 5_000


class Link:
    def __init__(self, model: LinkModel, rng: random.Random) -> None:
        self.model = model
        self._rng = rng
        self._last_delivery = 0
        self.sent = 0
        self.dropped = 0
        self.duplicated = 0

    def deliveries(self, now_us: int) -> list[int]:
        """Delivery times (true time) for one message sent at ``now_us``; empty if lost."""
        self.sent += 1
        model = self.model
        if any(w.contains(now_us) for w in model.partitions) or any(
            w.contains(now_us) and self._rng.random() < p for w, p in model.losses
        ):
            self.dropped += 1
            return []
        delay = model.base_us + self._rng.randint(0, max(model.jitter_us, 0))
        delay += sum(extra for window, extra in model.spikes if window.contains(now_us))
        at = now_us + delay
        if model.reorder_probability and self._rng.random() < model.reorder_probability:
            at += model.reorder_extra_us  # this one may overtake nothing but be overtaken
        else:
            at = max(at, self._last_delivery)  # in-order delivery
            self._last_delivery = at
        times = [at]
        if model.duplicate_probability and self._rng.random() < model.duplicate_probability:
            self.duplicated += 1
            times.append(at + self._rng.randint(1, max(model.jitter_us, 1) * 4))
        return times

    def prune(self, now_us: int) -> None:
        """Forget fault windows that have ended (keeps a long soak's link checks cheap)."""
        model = self.model
        model.partitions = [w for w in model.partitions if w.end_us > now_us]
        model.spikes = [(w, extra) for w, extra in model.spikes if w.end_us > now_us]
        model.losses = [(w, p) for w, p in model.losses if w.end_us > now_us]
