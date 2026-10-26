from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import PostgresDsn

from neura_control import ControlPlaneSettings, create_app
from neura_control import app as app_module
from neura_control.app import iter_routes
from neura_control.database import database_reachable
from neura_control.problems import PROBLEM_MEDIA_TYPE


def _client(*, db_ok: bool = True) -> TestClient:
    async def probe(url: PostgresDsn, timeout_s: float) -> bool:
        return db_ok

    return TestClient(create_app(ControlPlaneSettings(environment="test"), database_probe=probe))


def test_healthz() -> None:
    response = _client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_when_database_reachable() -> None:
    response = _client(db_ok=True).get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok"}


def test_readyz_returns_problem_when_database_unreachable() -> None:
    response = _client(db_ok=False).get("/readyz")
    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["reason"] == "API_UNAVAILABLE"
    assert body["retryable"] is True
    assert body["type"] == "urn:neura:reason-code:API_UNAVAILABLE"
    assert body["instance"] == "/readyz"


def test_meta_reports_versions() -> None:
    body = _client().get("/v1/meta").json()
    assert body["service"] == "neura-control"
    assert body["api_version"] == "v1"
    assert body["contracts_api_version"] == "neura.dev/v1alpha1"
    assert body["protocol_version"] == "PROTOCOL_VERSION_1"
    assert body["environment"] == "test"


def test_meta_matches_published_schema() -> None:
    client = _client()
    spec = yaml.safe_load(client.get("/v1/openapi.yaml").text)
    schema = spec["components"]["schemas"]["ServiceMeta"]
    body = client.get("/v1/meta").json()
    assert set(schema["required"]) <= set(body)
    assert set(body) <= set(schema["properties"])
    assert body["api_spec_version"] == spec["info"]["version"]


def test_unknown_route_is_a_problem() -> None:
    response = _client().get("/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["reason"] == "API_NOT_FOUND"


def test_unexpected_errors_do_not_leak_details() -> None:
    async def probe(url: PostgresDsn, timeout_s: float) -> bool:
        raise RuntimeError("secret connection string")

    app = create_app(ControlPlaneSettings(environment="test"), database_probe=probe)
    response = TestClient(app, raise_server_exceptions=False).get("/readyz")
    assert response.status_code == 500
    assert "secret" not in response.text
    assert response.json()["reason"] == "API_INTERNAL_ERROR"


def test_unexpected_errors_carry_a_request_id_and_are_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def probe(url: PostgresDsn, timeout_s: float) -> bool:
        raise RuntimeError("probe failed")

    app = create_app(ControlPlaneSettings(environment="test"), database_probe=probe)
    response = TestClient(app, raise_server_exceptions=False).get(
        "/readyz", headers={"X-Request-Id": "req-boom"}
    )
    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["reason"] == "API_INTERNAL_ERROR"
    assert response.headers["X-Request-Id"] == "req-boom"
    counted = [
        (s.labels["route"], s.labels["status"], s.value)
        for family in app.state.metrics.registry.collect()
        for s in family.samples
        if s.name == "neura_http_requests_total"
    ]
    assert counted == [("/readyz", "500", 1.0)]
    logged = [r for r in caplog.records if r.exc_info and "req-boom" in r.getMessage()]
    assert len(logged) == 1  # logged once, with the request id that ties it to the response


class FailingSessions:
    """A session factory whose database is gone: every use raises. Stops the loop after two."""

    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.uses = 0

    def __call__(self) -> Any:
        self.uses += 1
        if self.uses == 2:
            self.stop.set()
        raise OSError("connection refused")

    begin = __call__


@pytest.mark.parametrize("loop", ["_prune", "_sweep_rollouts"])
async def test_background_loops_log_a_failed_pass_and_run_the_next(
    loop: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(app_module, "PRUNE_INTERVAL_S", 0.01)
    settings = ControlPlaneSettings(environment="test", rollout_sweep_interval_s=0.01)
    stop = asyncio.Event()
    sessions = FailingSessions(stop)
    app = SimpleNamespace(state=SimpleNamespace(sessions=sessions))
    await asyncio.wait_for(getattr(app_module, loop)(app, settings, stop), timeout=10)
    assert sessions.uses == 2  # the first failure did not end the loop
    failures = [r for r in caplog.records if r.exc_info and r.exc_info[0] is OSError]
    assert len(failures) == 2


def test_implemented_routes_are_in_the_published_spec() -> None:
    """Spec-first: every public route the service exposes must be declared in api/openapi.yaml."""
    app = create_app(ControlPlaneSettings(environment="test"))
    spec = yaml.safe_load(ControlPlaneSettings().openapi_path.read_text(encoding="utf-8"))
    operational = {"/", "/healthz", "/readyz", "/v1/openapi.yaml", "/install.sh"}
    served = [(m, p) for m, p in iter_routes(app) if p not in operational]
    assert len(served) > 50
    for method, path in served:
        assert path in spec["paths"], f"{path} is served but not declared in the OpenAPI spec"
        assert method.lower() in spec["paths"][path], f"{method} {path} is not declared"


@pytest.mark.skipif(
    "NEURA_TEST_DATABASE_URL" not in os.environ,
    reason="set NEURA_TEST_DATABASE_URL to run against a real Postgres",
)
async def test_database_probe_against_real_postgres() -> None:
    url = PostgresDsn(os.environ["NEURA_TEST_DATABASE_URL"])
    assert await database_reachable(url, timeout_s=5)


async def test_database_probe_unreachable() -> None:
    url = PostgresDsn("postgresql://neura:neura@127.0.0.1:1/neura")
    assert not await database_reachable(url, timeout_s=1)
