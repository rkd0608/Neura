"""Generate the Grafana dashboards in deploy/observability/grafana/dashboards from the panel
definitions below, so their layout stays uniform and their queries are checked by the tests
(every metric they use must be one a component exports).

Usage:
    python tools/gen_dashboards.py           # write the dashboards
    python tools/gen_dashboards.py --check   # exit 1 if a dashboard is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy" / "observability" / "grafana" / "dashboards"
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}


@dataclass(frozen=True)
class Panel:
    title: str
    queries: list[tuple[str, str]]  # (PromQL, legend)
    unit: str = "short"
    kind: str = "timeseries"
    description: str = ""
    width: int = 8
    thresholds: list[tuple[float, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Row:
    title: str
    panels: list[Panel]


def rate(metric: str, by: str, window: str = "5m") -> str:
    return f"sum by ({by}) (rate({metric}[{window}]))"


SITE = [
    Row(
        "Robots",
        [
            Panel(
                "Robots served", [("sum(neura_gateway_sessions)", "robots")], kind="stat", width=4
            ),
            Panel(
                "Robots in fault",
                [("sum(neura_robot_faulted) or vector(0)", "faulted")],
                kind="stat",
                width=4,
                thresholds=[(0, "green"), (1, "red")],
            ),
            Panel(
                "Deadline miss rate",
                [
                    (
                        f"{rate('neura_scheduler_deadline_misses_total', 'group')} / "
                        f"clamp_min({rate('neura_scheduler_requests_total', 'group')}, 1e-9)",
                        "{{group}}",
                    )
                ],
                unit="percentunit",
                width=16,
                description="Share of inference requests finished after the robot's deadline "
                "(design §21.3). Each miss means the robot fell back to its buffer.",
            ),
            Panel(
                "Inference latency p99",
                [
                    (
                        "histogram_quantile(0.99, "
                        f"{rate('neura_inference_seconds_bucket', 'group, le')})",
                        "{{group}}",
                    )
                ],
                unit="s",
                width=12,
            ),
            Panel(
                "Robot round trip p99",
                [("neura_robot_rtt_p99_seconds", "{{robot}}")],
                unit="s",
                width=12,
            ),
            Panel(
                "Chunks per second",
                [("sum by (robot) (rate(neura_robot_chunks_total[1m]))", "{{robot}}")],
                unit="ops",
                width=12,
            ),
            Panel(
                "Chunks rejected by robots",
                [(rate("neura_robot_rejections_total", "reason"), "{{reason}}")],
                unit="ops",
                width=12,
            ),
        ],
    ),
    Row(
        "Serving",
        [
            Panel(
                "Mean batch size",
                [
                    (
                        f"{rate('neura_scheduler_batch_size_sum', 'group')} / "
                        f"clamp_min({rate('neura_scheduler_batch_size_count', 'group')}, 1e-9)",
                        "{{group}}",
                    )
                ],
                width=8,
            ),
            Panel(
                "Queued and in flight",
                [
                    ("sum by (group) (neura_scheduler_queued)", "queued {{group}}"),
                    ("sum by (group) (neura_scheduler_in_flight)", "in flight {{group}}"),
                ],
                width=8,
            ),
            Panel(
                "Requests dropped",
                [(rate("neura_scheduler_dropped_total", "reason"), "{{reason}}")],
                unit="ops",
                width=8,
            ),
            Panel(
                "Inference failures",
                [(rate("neura_inference_failures_total", "group"), "{{group}}")],
                unit="ops",
                width=8,
            ),
            Panel(
                "Worker restarts (1h)",
                [("sum by (group) (increase(neura_worker_restarts_total[1h]))", "{{group}}")],
                width=8,
            ),
            Panel(
                "Worker peak device memory",
                [("neura_worker_peak_memory_bytes", "{{group}}")],
                unit="bytes",
                width=8,
            ),
        ],
    ),
    Row(
        "Site agent",
        [
            Panel(
                "Connected to Neura",
                [("max(neura_agent_connected)", "connected")],
                kind="stat",
                width=4,
                thresholds=[(0, "red"), (1, "green")],
            ),
            Panel(
                "Offline authorization left",
                [("min(neura_agent_offline_authorization_remaining_seconds)", "remaining")],
                kind="stat",
                unit="s",
                width=4,
                thresholds=[(0, "red"), (3600, "orange"), (21600, "green")],
            ),
            Panel(
                "Approvals waiting",
                [("sum(neura_agent_pending_approvals)", "pending")],
                kind="stat",
                width=4,
            ),
            Panel(
                "Gateway restarts (1h)",
                [("sum(increase(neura_agent_gateway_restarts_total[1h]))", "restarts")],
                kind="stat",
                width=4,
            ),
            Panel(
                "Certificates: days left",
                [
                    (
                        "(neura_agent_certificate_expiry_timestamp_seconds - time()) / 86400",
                        "{{certificate}}",
                    ),
                    (
                        "(neura_gateway_certificate_expiry_timestamp_seconds - time()) / 86400",
                        "gateway (served)",
                    ),
                ],
                kind="stat",
                width=8,
                thresholds=[(0, "red"), (14, "orange"), (30, "green")],
            ),
            Panel(
                "Sent to Neura (bytes/s)",
                [(rate("neura_agent_outbound_bytes_total", "kind"), "{{kind}}")],
                unit="Bps",
                width=8,
                description="Everything the agent sends is in the outbound ledger "
                "(`neura-agent outbound`).",
            ),
            Panel(
                "Kept at the site",
                [
                    (
                        "sum(rate(neura_agent_outbound_withheld_fields_total[5m]))",
                        "fields withheld by the telemetry mode",
                    ),
                    (
                        "sum(rate(neura_agent_outbound_unknown_fields_total[5m]))",
                        "fields outside the schema (should be 0)",
                    ),
                ],
                unit="ops",
                width=8,
            ),
            Panel(
                "Operations (1h)",
                [("sum by (state) (increase(neura_agent_operations_total[1h]))", "{{state}}")],
                width=8,
            ),
            Panel(
                "Artifact signature checks (1h)",
                [
                    (
                        "sum by (result) (increase(neura_agent_artifact_checks_total[1h]))",
                        "{{result}}",
                    )
                ],
                width=8,
            ),
        ],
    ),
]

CONTROL_PLANE = [
    Row(
        "API",
        [
            Panel(
                "Requests by status",
                [(rate("neura_http_requests_total", "status"), "{{status}}")],
                unit="reqps",
                width=12,
            ),
            Panel(
                "Server errors",
                [
                    (
                        'sum(rate(neura_http_requests_total{status=~"5.."}[5m])) / '
                        "clamp_min(sum(rate(neura_http_requests_total[5m])), 1e-9)",
                        "5xx share",
                    )
                ],
                unit="percentunit",
                width=12,
            ),
            Panel(
                "Latency p99 by route",
                [
                    (
                        "histogram_quantile(0.99, "
                        f"{rate('neura_http_request_duration_seconds_bucket', 'route, le')})",
                        "{{route}}",
                    )
                ],
                unit="s",
                width=24,
            ),
        ],
    ),
    Row(
        "Sites",
        [
            Panel(
                "Agent streams",
                [("sum(neura_control_agent_streams)", "streams")],
                kind="stat",
                width=6,
            ),
            Panel(
                "Messages from sites",
                [(rate("neura_control_site_messages_total", "kind"), "{{kind}}")],
                unit="ops",
                width=9,
            ),
            Panel(
                "Enrollments (1h)",
                [
                    (
                        "sum by (result) (increase(neura_control_enrollments_total[1h]))",
                        "{{result}}",
                    )
                ],
                width=9,
            ),
            Panel(
                "Agent certificates issued (1d)",
                [("sum(increase(neura_control_agent_certificates_issued_total[1d]))", "issued")],
                kind="stat",
                width=6,
            ),
            Panel(
                "Sealed weights served",
                [("sum(rate(neura_control_sealed_weights_served_bytes_total[5m]))", "bytes/s")],
                unit="Bps",
                width=18,
            ),
        ],
    ),
]


def panel_json(panel: Panel, panel_id: int, x: int, y: int) -> dict[str, Any]:
    defaults: dict[str, Any] = {"unit": panel.unit}
    if panel.thresholds:
        defaults["thresholds"] = {
            "mode": "absolute",
            "steps": [
                {"color": color, "value": None if i == 0 else value}
                for i, (value, color) in enumerate(panel.thresholds)
            ],
        }
        defaults["color"] = {"mode": "thresholds"}
    body: dict[str, Any] = {
        "id": panel_id,
        "type": panel.kind,
        "title": panel.title,
        "datasource": DATASOURCE,
        "gridPos": {"h": 4 if panel.kind == "stat" else 8, "w": panel.width, "x": x, "y": y},
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "targets": [
            {
                "datasource": DATASOURCE,
                "expr": expr,
                "legendFormat": legend,
                "refId": chr(ord("A") + i),
            }
            for i, (expr, legend) in enumerate(panel.queries)
        ],
    }
    if panel.description:
        body["description"] = panel.description
    if panel.kind == "stat":
        body["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background"}
    return body


def dashboard(uid: str, title: str, description: str, rows: list[Row]) -> dict[str, Any]:
    panels: list[dict[str, Any]] = []
    y, panel_id = 0, 1
    for row in rows:
        panels.append(
            {
                "id": panel_id,
                "type": "row",
                "title": row.title,
                "collapsed": False,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
                "panels": [],
            }
        )
        panel_id += 1
        y += 1
        x, line_height = 0, 0
        for panel in row.panels:
            height = 4 if panel.kind == "stat" else 8
            if x + panel.width > 24:
                x, y = 0, y + line_height
                line_height = 0
            panels.append(panel_json(panel, panel_id, x, y))
            panel_id += 1
            x += panel.width
            line_height = max(line_height, height)
        y += line_height
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["neura"],
        "editable": True,
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "timezone": "browser",
        "templating": {"list": []},
        "annotations": {"list": []},
        "panels": panels,
    }


DASHBOARDS = {
    "neura-site.json": dashboard(
        "neura-site",
        "Neura site",
        "Robots, serving and the site agent, from the gateway's and the agent's /metrics.",
        SITE,
    ),
    "neura-control-plane.json": dashboard(
        "neura-control-plane",
        "Neura control plane",
        "The management API and the site-agent endpoint.",
        CONTROL_PLANE,
    ),
}


def rendered() -> dict[Path, str]:
    return {OUT / name: json.dumps(body, indent=2) + "\n" for name, body in DASHBOARDS.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if a dashboard is stale")
    args = parser.parse_args()
    outputs = rendered()
    if args.check:
        stale = [p for p, c in outputs.items() if not p.exists() or p.read_text() != c]
        for path in stale:
            print(f"{path.relative_to(ROOT)} is stale; run `make gen`", file=sys.stderr)
        return 1 if stale else 0
    OUT.mkdir(parents=True, exist_ok=True)
    for path, content in outputs.items():
        path.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
