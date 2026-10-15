"""Metrics for the customer's own monitoring (design §18.2): what the gateway exports while it
serves a robot, and the dashboards and alert rules built on the components' metrics."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from neura_agent.metrics import EXPORTED as AGENT
from neura_control.metrics import EXPORTED as CONTROL_PLANE
from neura_runtime.grpc_client import RobotCredentials
from neura_runtime.runtime import RuntimeState
from neura_server.config import (
    DeploymentConfig,
    DeviceConfig,
    GatewayConfig,
    ServingConfig,
)
from neura_server.grpc_server import GatewayServer
from neura_server.metrics import EXPORTED as GATEWAY
from neura_sim.grpc_robots import SimulatedRobotProcess, load_binding

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "contracts"
OBSERVABILITY = ROOT / "deploy" / "observability"
ARTIFACT = EXAMPLES / "act-so101.artifact.yaml"
ROBOT = EXAMPLES / "so101.robot.yaml"
PROFILE = EXAMPLES / "act-so101-30hz.profile.yaml"
TOKEN = "observability-test-token"


def samples(exposition: bytes) -> dict[str, list[tuple[dict[str, str], float]]]:
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for family in text_string_to_metric_families(exposition.decode()):
        for sample in family.samples:
            out.setdefault(sample.name, []).append((sample.labels, sample.value))
    return out


def families(exposition: bytes) -> set[str]:
    return {f.name for f in text_string_to_metric_families(exposition.decode())}


async def test_the_gateway_exports_what_it_serves() -> None:
    config = GatewayConfig(
        listen="127.0.0.1:0",
        status_listen=None,
        devices=[
            DeviceConfig(robot_id="arm-1", token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest())
        ],
        deployments=[
            DeploymentConfig(
                revision_id="rev_1",
                artifact=ARTIFACT,
                robot_profile=ROBOT,
                execution_profile=PROFILE,
                robots=["arm-1"],
            )
        ],
        serving=ServingConfig(backend="simulated", simulate_latency=False),
    )
    server = GatewayServer(config)
    await server.start()
    robot = SimulatedRobotProcess(
        load_binding(ARTIFACT, ROBOT, PROFILE),
        "arm-1",
        "rev_1",
        f"127.0.0.1:{server.bound_port}",
        RobotCredentials(token=TOKEN),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(robot.connection.run(stop))
    try:
        for _ in range(200):
            status = server.status()
            if robot.runtime.state is RuntimeState.RUNNING and any(
                s["chunks"] >= 3 for s in status["sessions"]
            ):
                break
            await asyncio.sleep(0.05)
        exposition = TestClient(server.status_app()).get("/metrics").content
    finally:
        stop.set()
        await task
        await server.stop()
    exported = families(exposition)
    assert exported <= GATEWAY
    # Without TLS there is no server certificate; nothing else may be missing.
    assert GATEWAY - exported == {"neura_gateway_certificate_expiry_timestamp_seconds"}
    values = samples(exposition)
    assert values["neura_gateway_sessions"] == [({"revision": "rev_1"}, 1.0)]
    chunks = values["neura_robot_chunks_total"]
    assert chunks[0][0] == {"robot": "arm-1", "revision": "rev_1"}
    assert chunks[0][1] >= 3
    inference = {labels["group"]: v for labels, v in values["neura_inference_seconds_count"]}
    batches = {labels["group"]: v for labels, v in values["neura_scheduler_batches_total"]}
    assert inference == batches
    assert all(v >= 3 for v in batches.values())
    sizes = values["neura_scheduler_batch_size_count"]
    assert [v for _, v in sizes] == list(batches.values())


# --- dashboards and alert rules ------------------------------------------------------------------

METRIC = re.compile(r"\bneura_[a-z0-9_]+")
SUFFIXES = ("_bucket", "_count", "_sum", "_total")


def used(expression: str) -> set[str]:
    names = set()
    for name in METRIC.findall(expression):
        for suffix in SUFFIXES:
            if name.endswith(suffix) and name.removesuffix(suffix) in GATEWAY | AGENT | (
                CONTROL_PLANE
            ):
                name = name.removesuffix(suffix)
                break
        names.add(name)
    return names


def test_dashboards_only_use_metrics_the_components_export() -> None:
    exported = GATEWAY | AGENT | CONTROL_PLANE
    dashboards = sorted((OBSERVABILITY / "grafana" / "dashboards").glob("*.json"))
    assert [d.name for d in dashboards] == ["neura-control-plane.json", "neura-site.json"]
    for path in dashboards:
        dashboard = json.loads(path.read_text())
        panels = [p for p in dashboard["panels"] if p["type"] != "row"]
        assert panels, path.name
        ids = [p["id"] for p in dashboard["panels"]]
        assert len(ids) == len(set(ids)), path.name
        for panel in panels:
            assert panel["datasource"]["uid"] == "prometheus"
            for target in panel["targets"]:
                missing = used(target["expr"]) - exported
                assert not missing, (path.name, panel["title"], missing)


def test_alert_rules_only_use_metrics_the_components_export() -> None:
    exported = GATEWAY | AGENT | CONTROL_PLANE
    rules = yaml.safe_load((OBSERVABILITY / "rules" / "neura.yml").read_text())
    alerts = [r for group in rules["groups"] for r in group["rules"]]
    assert len({a["alert"] for a in alerts}) == len(alerts)
    for alert in alerts:
        assert alert["annotations"]["summary"]
        missing = used(alert["expr"]) - exported
        assert not missing, (alert["alert"], missing)


def test_prometheus_scrapes_every_component() -> None:
    config = yaml.safe_load((OBSERVABILITY / "prometheus.yml").read_text())
    targets = {
        job["job_name"]: job["static_configs"][0]["targets"] for job in config["scrape_configs"]
    }
    assert targets == {
        "neura-control-plane": ["control-plane:9464"],
        "neura-site-agent": ["site-agent:8090"],
        "neura-gateway": ["site-agent:8091"],
    }
    compose = yaml.safe_load((ROOT / "deploy" / "compose" / "compose.yaml").read_text())
    control = compose["services"]["control-plane"]["environment"]
    assert control["NEURA_CONTROL_METRICS_LISTEN"].endswith(":9464")
