"""Prometheus metrics for the gateway, on its status listener at ``/metrics`` (design §18.2).

Metrics are for the customer's own monitoring: they stay at the site unless the customer scrapes
them elsewhere. Most are read from the gateway's own counters at scrape time, so nothing is
counted twice; inference latency is observed as batches complete.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector

# No `_created` series: they double the series count for nothing Neura's dashboards use.
disable_created_metrics()  # type: ignore[no-untyped-call]

# Every metric family the gateway exports (counters without their _total suffix).
EXPORTED = frozenset(
    {
        "neura_inference_seconds",
        "neura_inference_failures",
        "neura_gateway_uptime_seconds",
        "neura_gateway_streams",
        "neura_gateway_sessions",
        "neura_robot_rtt_p99_seconds",
        "neura_robot_chunks",
        "neura_robot_observations",
        "neura_robot_queued_actions",
        "neura_robot_faulted",
        "neura_robot_rejections",
        "neura_scheduler_queued",
        "neura_scheduler_in_flight",
        "neura_scheduler_admitted_sessions",
        "neura_scheduler_batches",
        "neura_scheduler_requests",
        "neura_scheduler_deadline_misses",
        "neura_scheduler_starved_on_arrival",
        "neura_scheduler_failed",
        "neura_scheduler_dropped",
        "neura_scheduler_batch_size",
        "neura_worker_ready",
        "neura_worker_restarts",
        "neura_worker_peak_memory_bytes",
        "neura_gateway_events",
        "neura_gateway_certificate_expiry_timestamp_seconds",
    }
)

LATENCY_BUCKETS = (0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5, 5.0)
BATCH_BUCKETS = (1, 2, 4, 8, 16, 32, 64, 128, 256)


class GatewayMetrics:
    def __init__(
        self, status: Callable[[], dict[str, Any]], certificate: Path | None = None
    ) -> None:
        self.registry = CollectorRegistry()
        self.inference = Histogram(
            "neura_inference_seconds",
            "Time from dispatching a batch to its outputs, per batch group.",
            ["group"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.inference_failures = Counter(
            "neura_inference_failures",
            "Batches whose inference failed (worker crash, timeout or error).",
            ["group"],
            registry=self.registry,
        )
        self.registry.register(_StatusCollector(status, certificate))

    def batch_done(self, group: str, seconds: float, *, failed: bool) -> None:
        self.inference.labels(group).observe(seconds)
        if failed:
            self.inference_failures.labels(group).inc()

    def exposition(self) -> bytes:
        return generate_latest(self.registry)


def certificate_expiry(path: Path) -> float | None:
    from neura_contracts import pki

    try:
        return pki.not_after(path.read_bytes()).timestamp()
    except (OSError, ValueError):
        return None


class _StatusCollector(Collector):
    def __init__(self, status: Callable[[], dict[str, Any]], certificate: Path | None) -> None:
        self._status = status
        self._certificate = certificate

    def describe(self) -> list[Metric]:
        return []  # collected on demand: the families depend on what the gateway serves

    def collect(self) -> Iterator[Metric]:
        status = self._status()
        uptime = GaugeMetricFamily("neura_gateway_uptime_seconds", "Seconds since start.")
        uptime.add_metric([], float(status.get("uptime_s", 0)))
        yield uptime
        streams = GaugeMetricFamily("neura_gateway_streams", "Open robot streams.")
        streams.add_metric([], float(status.get("streams", 0)))
        yield streams

        sessions = status.get("sessions", [])
        by_revision: dict[str, int] = {}
        for s in sessions:
            by_revision[s["revision_id"]] = by_revision.get(s["revision_id"], 0) + 1
        per_revision = GaugeMetricFamily(
            "neura_gateway_sessions", "Robot sessions served, per revision.", labels=["revision"]
        )
        for revision, count in sorted(by_revision.items()):
            per_revision.add_metric([revision], count)
        yield per_revision

        labels = ["robot", "revision"]
        rtt = GaugeMetricFamily(
            "neura_robot_rtt_p99_seconds", "Robot round trip, 99th percentile.", labels=labels
        )
        chunks = CounterMetricFamily("neura_robot_chunks", "Action chunks sent.", labels=labels)
        observations = CounterMetricFamily(
            "neura_robot_observations", "Observations received.", labels=labels
        )
        queued = GaugeMetricFamily(
            "neura_robot_queued_actions",
            "Actions the robot has buffered, as last reported.",
            labels=labels,
        )
        faulted = GaugeMetricFamily(
            "neura_robot_faulted", "1 while the robot reports a fault.", labels=[*labels, "reason"]
        )
        rejections = CounterMetricFamily(
            "neura_robot_rejections",
            "Chunks the robot rejected, by reason.",
            labels=["robot", "reason"],
        )
        for s in sessions:
            key = [s["robot_id"], s["revision_id"]]
            rtt.add_metric(key, float(s.get("rtt_p99_us") or 0) / 1e6)
            chunks.add_metric(key, float(s.get("chunks", 0)))
            observations.add_metric(key, float(s.get("observations", 0)))
            queued.add_metric(key, float(s.get("queued_actions", 0)))
            if s.get("fault_reason"):
                faulted.add_metric([*key, s["fault_reason"]], 1)
            for reason, count in sorted((s.get("rejections") or {}).items()):
                rejections.add_metric([s["robot_id"], reason], float(count))
        yield from (rtt, chunks, observations, queued, faulted, rejections)

        group = ["group"]
        gauges = {
            "queued": ("neura_scheduler_queued", "Requests waiting for a batch."),
            "in_flight": ("neura_scheduler_in_flight", "Requests in a running batch."),
            "admitted_sessions": ("neura_scheduler_admitted_sessions", "Sessions admitted."),
        }
        counters = {
            "batches": ("neura_scheduler_batches", "Batches dispatched."),
            "dispatched_requests": ("neura_scheduler_requests", "Requests dispatched."),
            "deadline_misses": (
                "neura_scheduler_deadline_misses",
                "Requests finished after their robot's deadline.",
            ),
            "starved_on_arrival": (
                "neura_scheduler_starved_on_arrival",
                "Requests from robots already starved when they asked.",
            ),
            "failed": ("neura_scheduler_failed", "Requests whose batch failed."),
        }
        families: dict[str, Any] = {
            key: GaugeMetricFamily(name, doc, labels=group) for key, (name, doc) in gauges.items()
        }
        families |= {
            key: CounterMetricFamily(name, doc, labels=group)
            for key, (name, doc) in counters.items()
        }
        dropped = CounterMetricFamily(
            "neura_scheduler_dropped", "Requests dropped, by reason.", labels=["group", "reason"]
        )
        batch_size = HistogramMetricFamily(
            "neura_scheduler_batch_size", "Requests per dispatched batch.", labels=group
        )
        worker_ready = GaugeMetricFamily(
            "neura_worker_ready", "1 while the group's worker is ready.", labels=group
        )
        worker_restarts = CounterMetricFamily(
            "neura_worker_restarts", "Worker process restarts.", labels=group
        )
        worker_memory = GaugeMetricFamily(
            "neura_worker_peak_memory_bytes", "Peak device memory of the worker.", labels=group
        )
        for key, g in sorted(status.get("groups", {}).items()):
            for field, family in families.items():
                family.add_metric([key], float(g.get(field, 0)))
            for reason, count in sorted((g.get("dropped") or {}).items()):
                dropped.add_metric([key, reason], float(count))
            sizes = {int(k): int(v) for k, v in (g.get("batch_sizes") or {}).items()}
            cumulative, buckets = 0, []
            for bound in BATCH_BUCKETS:
                cumulative = sum(c for size, c in sizes.items() if size <= bound)
                buckets.append((str(float(bound)), float(cumulative)))
            buckets.append(("+Inf", float(sum(sizes.values()))))
            batch_size.add_metric(
                [key], buckets, sum_value=float(sum(s * c for s, c in sizes.items()))
            )
            worker = g.get("worker") or {}
            worker_ready.add_metric([key], 1.0 if worker.get("state") == "ready" else 0.0)
            worker_restarts.add_metric([key], float(worker.get("restarts", 0)))
            if worker.get("peak_memory_mib") is not None:
                worker_memory.add_metric([key], float(worker["peak_memory_mib"]) * 2**20)
        yield from families.values()
        yield from (dropped, batch_size, worker_ready, worker_restarts, worker_memory)

        events = CounterMetricFamily(
            "neura_gateway_events",
            "Gateway events: sessions opened and refusals, by reason code.",
            labels=["event"],
        )
        for event, count in sorted((status.get("counters") or {}).items()):
            events.add_metric([event], float(count))
        yield events

        if self._certificate is not None:
            expiry = certificate_expiry(self._certificate)
            if expiry is not None:
                family = GaugeMetricFamily(
                    "neura_gateway_certificate_expiry_timestamp_seconds",
                    "When the gateway's TLS server certificate expires.",
                )
                family.add_metric([], expiry)
                yield family
