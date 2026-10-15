"""The control plane's Prometheus metrics, on their own listener (design §18.2)."""

from __future__ import annotations

import socket

import httpx
from prometheus_client.parser import text_string_to_metric_families

from conftest import AppFactory
from neura_control.metrics import EXPORTED


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def test_requests_are_counted_by_route_template_on_a_separate_listener(
    make_api: AppFactory,
) -> None:
    port = free_port()
    client, org = make_api(settings_overrides={"metrics_listen": f"127.0.0.1:{port}"})
    project = client.post(
        f"/v1/organizations/{org}/projects", {"spec": {"name": "m", "display_name": "M"}}
    ).json()["metadata"]["id"]
    assert client.get(f"/v1/projects/{project}/sites").status_code == 200
    assert client.get("/v1/projects/prj_01J0000000000000000000000X").status_code == 404
    assert client.get("/metrics", validate=False).status_code == 404  # not on the public API
    exposition = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5).text
    families = {f.name: f for f in text_string_to_metric_families(exposition)}
    assert set(families) <= EXPORTED
    requests = [
        (s.labels["route"], s.labels["method"], s.labels["status"], s.value)
        for s in families["neura_http_requests"].samples
        if s.name == "neura_http_requests_total"
    ]
    assert ("/v1/projects/{project_id}/sites", "GET", "200", 1.0) in requests
    assert ("/v1/projects/{project_id}", "GET", "404", 1.0) in requests
    # Identifiers never become label values.
    assert not any(project in route or "prj_" in route for route, *_ in requests)
