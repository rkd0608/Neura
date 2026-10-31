"""How the site's processes are supervised: one source for the timeouts each supervisor waits
when it stops a process (so whoever stops a process waits longer than that process takes to stop
its own), and the backoff each uses to restart one that exited (robots, gateways, workers)."""

from __future__ import annotations

import time
from collections.abc import Callable

# A policy worker that is serving has this long to exit once its stdin closes, then it is killed.
# One that has not announced ready (it may be loading for minutes) is killed at once.
WORKER_STOP_TIMEOUT_S = 5.0
# Removing a worker's container by name (a worker whose supervisor killed it, or that hung).
CONTAINER_REMOVE_TIMEOUT_S = 30.0
# The gateway's status server finishing its requests when the gateway stops.
STATUS_STOP_TIMEOUT_S = 5.0
# The longest a gateway takes to stop: its status server, then its workers, stopped together, each
# then waiting for its container's removal, with a margin for the rest. Whoever stops a gateway
# (the site agent) waits this long before killing it, which would leave its workers behind.
GATEWAY_STOP_TIMEOUT_S = (
    STATUS_STOP_TIMEOUT_S + WORKER_STOP_TIMEOUT_S + CONTAINER_REMOVE_TIMEOUT_S + 5.0
)
# A process that ran this long before it exited failed anew: it is not in a crash loop.
HEALTHY_RUN_S = 30.0


class RestartBackoff:
    """How long to wait before restarting a process that exited: ``first_s``, doubling with each
    exit up to ``max_s``, and ``first_s`` again after a healthy run (HEALTHY_RUN_S). How many times
    it restarted is the supervisor's to count."""

    def __init__(
        self, first_s: float, max_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.first_s = first_s
        self.max_s = max_s
        self._clock = clock
        self._next_s = first_s
        self._started = clock()

    def started(self) -> None:
        self._started = self._clock()

    def exited(self) -> float:
        """The wait before the next start."""
        if self._clock() - self._started > HEALTHY_RUN_S:
            self._next_s = self.first_s
        delay, self._next_s = self._next_s, min(self._next_s * 2, self.max_s)
        return delay
