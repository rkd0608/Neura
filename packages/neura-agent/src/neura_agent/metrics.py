"""Prometheus metrics for the site agent, on its local API at ``/metrics`` (design §18.2).

For the customer's own monitoring: connection and offline authorization, desired state, local
approvals, operations, artifact checks, what was sent to Neura, and when certificates expire.
Read from the agent's state at scrape time.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, disable_created_metrics, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from neura_contracts import pki

if TYPE_CHECKING:
    from neura_agent.runner import AgentRuntime


# No `_created` series: they double the series count for nothing Neura's dashboards use.
disable_created_metrics()  # type: ignore[no-untyped-call]

# Every metric family the agent exports (counters without their _total suffix).
EXPORTED = frozenset(
    {
        "neura_agent_info",
        "neura_agent_enrolled",
        "neura_agent_outbound_messages",
        "neura_agent_outbound_bytes",
        "neura_agent_outbound_withheld_fields",
        "neura_agent_outbound_unknown_fields",
        "neura_agent_outbound_withheld_messages",
        "neura_agent_connected",
        "neura_agent_offline_authorization_remaining_seconds",
        "neura_agent_authorization_expired",
        "neura_agent_manifest_generation",
        "neura_agent_prepared_revisions",
        "neura_agent_pending_approvals",
        "neura_agent_outbox_messages",
        "neura_agent_gateway_running",
        "neura_agent_gateway_suspended",
        "neura_agent_gateway_restarts",
        "neura_agent_operations",
        "neura_agent_artifact_checks",
        "neura_agent_robot_certificates",
        "neura_agent_certificate_expiry_timestamp_seconds",
    }
)


class AgentMetrics:
    def __init__(self, runtime: AgentRuntime, version: str) -> None:
        self.registry = CollectorRegistry()
        self.registry.register(_AgentCollector(runtime, version))

    def exposition(self) -> bytes:
        return generate_latest(self.registry)


def _expiry(path: Path) -> float | None:
    try:
        return pki.not_after(path.read_bytes()).timestamp()
    except (OSError, ValueError):
        return None


def _gauge(name: str, doc: str, value: float) -> GaugeMetricFamily:
    family = GaugeMetricFamily(name, doc)
    family.add_metric([], value)
    return family


class _AgentCollector(Collector):
    def __init__(self, runtime: AgentRuntime, version: str) -> None:
        self._runtime = runtime
        self._version = version

    def describe(self) -> list[Metric]:
        return []

    def collect(self) -> Iterator[Metric]:
        runtime = self._runtime
        settings = runtime.settings
        identity = runtime.identities.load()
        info = GaugeMetricFamily(
            "neura_agent_info",
            "The agent: version, telemetry mode and identity.",
            labels=["version", "telemetry_mode", "site_id", "server_id"],
        )
        info.add_metric(
            [
                self._version,
                settings.telemetry_mode,
                identity.site_id if identity else "",
                identity.server_id if identity else "",
            ],
            1,
        )
        yield info
        yield _gauge(
            "neura_agent_enrolled", "1 once the server is enrolled.", float(bool(identity))
        )

        gate = runtime.outbound
        messages = CounterMetricFamily(
            "neura_agent_outbound_messages", "Messages sent to Neura, by kind.", labels=["kind"]
        )
        sent_bytes = CounterMetricFamily(
            "neura_agent_outbound_bytes", "Bytes sent to Neura, by kind.", labels=["kind"]
        )
        for kind, count in sorted(gate.sent.items()):
            messages.add_metric([kind], count)
            sent_bytes.add_metric([kind], gate.sent_bytes.get(kind, 0))
        yield messages
        yield sent_bytes
        withheld = CounterMetricFamily(
            "neura_agent_outbound_withheld_fields",
            "Fields the telemetry mode kept at the site.",
            value=gate.withheld_fields,
        )
        unknown = CounterMetricFamily(
            "neura_agent_outbound_unknown_fields",
            "Fields outside the outbound schema, never sent (should stay 0).",
            value=gate.unknown_fields,
        )
        whole = CounterMetricFamily(
            "neura_agent_outbound_withheld_messages",
            "Messages the telemetry mode kept at the site entirely.",
            value=gate.withheld_messages,
        )
        yield from (withheld, unknown, whole)

        core = runtime.core
        if core is None:
            return
        connection = runtime.connection
        yield _gauge(
            "neura_agent_connected",
            "1 while the stream to the control plane is up.",
            float(bool(connection and connection.connected)),
        )
        remaining = core.offline_remaining_s()
        if remaining is not None:
            yield _gauge(
                "neura_agent_offline_authorization_remaining_seconds",
                "Offline authorization left before serving stops (design §13).",
                max(remaining, 0.0),
            )
        yield _gauge(
            "neura_agent_authorization_expired",
            "1 while offline authorization has run out.",
            float(core.authorization_expired()),
        )
        yield _gauge(
            "neura_agent_manifest_generation",
            "Desired-state generation committed at the site.",
            float(core.state.manifest_generation),
        )
        yield _gauge(
            "neura_agent_prepared_revisions", "Revisions prepared.", float(len(core.state.prepared))
        )
        yield _gauge(
            "neura_agent_pending_approvals",
            "Activations waiting for local approval.",
            float(len(core.pending_approvals())),
        )
        yield _gauge(
            "neura_agent_outbox_messages",
            "Messages queued for the control plane.",
            float(core.outbox.qsize()),
        )
        yield _gauge(
            "neura_agent_gateway_running", "1 while the gateway runs.", float(core.gateway.running)
        )
        yield _gauge(
            "neura_agent_gateway_suspended",
            "1 while serving is suspended (offline authorization ran out).",
            float(core.gateway_suspended),
        )
        yield CounterMetricFamily(
            "neura_agent_gateway_restarts",
            "Times the supervised gateway process was restarted.",
            value=float(getattr(core.gateway, "restarts", 0)),
        )
        operations = CounterMetricFamily(
            "neura_agent_operations", "Operations finished at the site, by state.", labels=["state"]
        )
        for state, count in sorted(core.operations.items()):
            operations.add_metric([state], count)
        yield operations
        checks = CounterMetricFamily(
            "neura_agent_artifact_checks",
            "Publisher-signature checks at prepare: verified, or the reason code.",
            labels=["result"],
        )
        for result, count in sorted(core.artifact_checks.items()):
            checks.add_metric([result], count)
        yield checks

        now = datetime.datetime.now(datetime.UTC)
        robots = sum(
            1
            for records in core.state.robot_certificates.values()
            for r in records
            if datetime.datetime.fromisoformat(r["not_after"]) > now
        )
        yield _gauge("neura_agent_robot_certificates", "Unexpired robot certificates.", robots)
        expiry = GaugeMetricFamily(
            "neura_agent_certificate_expiry_timestamp_seconds",
            "When each certificate this site relies on expires.",
            labels=["certificate"],
        )
        state_dir = settings.state_dir
        for name, path in (
            ("site_ca", state_dir / "pki" / "ca.pem"),
            ("gateway", core.gateway_dir / "tls" / "server.pem"),
            ("agent_client", state_dir / "tls" / "client.pem"),
        ):
            when = _expiry(path)
            if when is not None:
                expiry.add_metric([name], when)
        yield expiry
