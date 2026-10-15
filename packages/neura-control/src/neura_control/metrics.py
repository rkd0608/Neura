"""Prometheus metrics for the control plane, served on their own listener (``metrics_listen``),
apart from the public API.

HTTP requests by route template, method and status, with latency; agent streams; enrollments,
site reports, certificates issued and sealed weights served.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, MutableMapping
from typing import Any
from wsgiref.simple_server import WSGIServer

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    disable_created_metrics,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

# No `_created` series: they double the series count for nothing Neura's dashboards use.
disable_created_metrics()  # type: ignore[no-untyped-call]

# Every metric family the control plane exports (counters without their _total suffix).
EXPORTED = frozenset(
    {
        "neura_http_requests",
        "neura_http_request_duration_seconds",
        "neura_control_enrollments",
        "neura_control_site_messages",
        "neura_control_agent_certificates_issued",
        "neura_control_sealed_weights_served_bytes",
        "neura_control_agent_streams",
    }
)

HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class ControlPlaneMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "neura_http_requests",
            "API requests, by route template, method and status.",
            ["route", "method", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "neura_http_request_duration_seconds",
            "API request latency, by route template and method.",
            ["route", "method"],
            buckets=HTTP_BUCKETS,
            registry=self.registry,
        )
        self.enrollments = Counter(
            "neura_control_enrollments",
            "Server enrollments, by result.",
            ["result"],
            registry=self.registry,
        )
        self.site_messages = Counter(
            "neura_control_site_messages",
            "Messages received from site agents, by kind.",
            ["kind"],
            registry=self.registry,
        )
        self.certificates = Counter(
            "neura_control_agent_certificates_issued",
            "TLS client certificates issued to site agents.",
            registry=self.registry,
        )
        self.sealed_bytes = Counter(
            "neura_control_sealed_weights_served_bytes",
            "Bytes of sealed weights served to site agents.",
            registry=self.registry,
        )
        self._streams: Callable[[], int] = lambda: 0
        self.registry.register(_Streams(lambda: self._streams()))

    def count_streams(self, streams: Callable[[], int]) -> None:
        self._streams = streams

    def request(self, route: str, method: str, status: int, seconds: float) -> None:
        self.requests.labels(route, method, str(status)).inc()
        self.latency.labels(route, method).observe(seconds)

    def serve(self, listen: str) -> tuple[WSGIServer, threading.Thread]:
        host, _, port = listen.rpartition(":")
        any_address = "0.0.0.0"  # noqa: S104 - only when the operator lists no host
        return start_http_server(int(port), addr=host or any_address, registry=self.registry)


class _Streams(Collector):
    def __init__(self, count: Callable[[], int]) -> None:
        self._count = count

    def describe(self) -> list[Metric]:
        return []

    def collect(self) -> Iterator[Metric]:
        family = GaugeMetricFamily(
            "neura_control_agent_streams", "Site-agent streams open on this process."
        )
        family.add_metric([], float(self._count()))
        yield family


def route_template(scope: MutableMapping[str, Any]) -> str:
    """The matched route's path template, so ids do not become label values."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"
