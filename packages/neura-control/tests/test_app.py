from __future__ import annotations

import os

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import PostgresDsn

from neura_control import ControlPlaneSettings, create_app
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


def test_implemented_routes_are_in_the_published_spec() -> None:
    """Spec-first: every public route the service exposes must be declared in api/openapi.yaml."""
    app = create_app(ControlPlaneSettings(environment="test"))
    spec = yaml.safe_load(ControlPlaneSettings().openapi_path.read_text(encoding="utf-8"))
    operational = {"/", "/healthz", "/readyz", "/v1/openapi.yaml"}
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None or path in operational:
            continue
        assert path in spec["paths"], f"{path} is served but not declared in the OpenAPI spec"


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
