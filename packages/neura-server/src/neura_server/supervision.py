"""How long the data plane's processes take to stop: one source for the timeouts each supervisor
waits, so whoever stops a process waits longer than that process takes to stop its own."""

from __future__ import annotations

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
