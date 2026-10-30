"""Control-plane test fixtures.

Database tests need PostgreSQL: set ``NEURA_TEST_DATABASE_URL`` (CI does). Each test run creates a
throwaway database, migrates it, and truncates every table before each test.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import os
import secrets
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2 as httpx
import jwt
import psycopg
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from neura_control.app import create_app
from neura_control.db.engine import create_engine, session_factory
from neura_control.db.migrate import upgrade
from neura_control.security.oidc import OidcVerifier
from neura_control.security.principal import Role
from neura_control.services import bootstrap
from neura_control.settings import ControlPlaneSettings

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spec_validator import SpecValidator

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"
VALIDATOR = SpecValidator()


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    base = os.environ.get("NEURA_TEST_DATABASE_URL")
    if not base:
        pytest.skip("set NEURA_TEST_DATABASE_URL to run control-plane database tests")
    name = f"neura_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = _with_database(base, name)
    upgrade(url)
    try:
        yield url
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


TABLES = [
    "login_sessions",
    "publisher_keys",
    "rollouts",
    "policy_versions",
    "policies",
    "idempotency_keys",
    "events",
    "events_pruned",
    "audit_records",
    "metric_points",
    "faults",
    "robot_sessions",
    "site_commands",
    "activation_requests",
    "evaluations",
    "training_jobs",
    "operations",
    "revisions",
    "deployments",
    "robots",
    "contract_documents",
    "servers",
    "enrollment_tokens",
    "support_grants",
    "sites",
    "projects",
    "api_tokens",
    "memberships",
    "organizations",
]


@pytest.fixture
def clean_database(database_url: str) -> str:
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    return database_url


class ConformanceClient:
    """A TestClient wrapper that checks every response against the OpenAPI spec."""

    def __init__(self, client: TestClient, token: str | None) -> None:
        self.client = client
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        idempotency_key: str | bool | None = True,
        token: str | bool | None = True,
        content_type: str | None = None,
        params: Any = None,
        validate: bool = True,
        content: bytes | None = None,
    ) -> httpx.Response:
        merged: dict[str, str] = dict(headers or {})
        bearer = self.token if token is True else token
        if bearer:
            merged["Authorization"] = f"Bearer {bearer}"
        if method == "POST" and idempotency_key is not False and idempotency_key is not None:
            merged["Idempotency-Key"] = (
                secrets.token_hex(16) if idempotency_key is True else str(idempotency_key)
            )
        if json is not None:
            merged.setdefault(
                "Content-Type",
                content_type
                or ("application/merge-patch+json" if method == "PATCH" else "application/json"),
            )
        if content is not None:
            merged.setdefault("Content-Type", content_type or "application/octet-stream")
        elif json is not None:
            content = jsonlib.dumps(json).encode()
        response = self.client.request(method, path, content=content, headers=merged, params=params)
        if validate and path.startswith("/v1/"):
            VALIDATOR.validate(
                method,
                path,
                response.status_code,
                response.headers.get("content-type", ""),
                response.content,
            )
        return response

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        return self.request("POST", path, json=json if json is not None else {}, **kw)

    def patch(self, path: str, json: Any, *, version: str | None, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}) or {})
        if version is not None:
            headers["If-Match"] = f'"{version}"'
        return self.request("PATCH", path, json=json, headers=headers, **kw)

    def delete(self, path: str, *, version: str | None, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}) or {})
        if version is not None:
            headers["If-Match"] = f'"{version}"'
        return self.request("DELETE", path, headers=headers, **kw)

    def as_token(self, token: str | None) -> ConformanceClient:
        return ConformanceClient(self.client, token)

    @property
    def app(self) -> FastAPI:
        """The application under test, for what it holds in ``app.state``."""
        app = self.client.app
        assert isinstance(app, FastAPI)
        return app


async def _bootstrap(url: str, role_subjects: dict[str, Role]) -> tuple[str, str]:
    engine = create_engine(url, pool_size=1)
    try:
        async with session_factory(engine).begin() as session:
            org = await bootstrap.ensure_organization(session, "Acme Robotics")
            for subject, role in role_subjects.items():
                await bootstrap.add_membership(session, org.id, subject, role)
            token = await bootstrap.create_api_token(
                session, org.id, display_name="test admin", scopes=["admin"]
            )
            return org.id, token
    finally:
        await engine.dispose()


@pytest.fixture
def settings(clean_database: str) -> ControlPlaneSettings:
    return ControlPlaneSettings(
        environment="test",
        database_url=clean_database,  # type: ignore[arg-type]
        rate_limit_per_minute=100_000,
        events_poll_interval_s=0.05,
        events_keepalive_s=0.2,
        events_max_stream_s=0.5,
        agent_grpc_listen=None,
    )


class OidcIssuer:
    """A test SSO issuer: signs OIDC access tokens the control plane is configured to trust."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.verifier = OidcVerifier(
            issuer="https://sso.example.com",
            audience="neura",
            key_resolver=lambda _: self.key.public_key(),
        )

    def token(
        self, sub: str, *, audience: str = "neura", expires_in: int = 300, **claims: Any
    ) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "iss": "https://sso.example.com",
                "sub": sub,
                "aud": audience,
                "iat": now,
                "exp": now + timedelta(seconds=expires_in),
                **claims,
            },
            self.key,
            algorithm="RS256",
        )


AppFactory = Callable[..., tuple[ConformanceClient, str]]


@pytest.fixture
def make_api(settings: ControlPlaneSettings) -> Iterator[AppFactory]:
    clients: list[TestClient] = []

    def factory(**app_kwargs: Any) -> tuple[ConformanceClient, str]:
        subjects = app_kwargs.pop("memberships", {})
        overrides = app_kwargs.pop("settings_overrides", {})
        app_settings = settings.model_copy(update=overrides) if overrides else settings
        org_id, token = asyncio.run(_bootstrap(str(app_settings.database_url), subjects))
        client = TestClient(create_app(app_settings, **app_kwargs), raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return ConformanceClient(client, token), org_id

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def api(make_api: AppFactory) -> tuple[ConformanceClient, str]:
    return make_api()


async def until(condition: Callable[[], Any], what: str, timeout: float = 10) -> Any:
    """Poll ``condition`` without blocking the event loop until it is true, and return it: a
    test waits for the state it needs, never for a fixed time."""
    deadline = time.monotonic() + timeout
    while not (value := condition()):
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.02)
    return value


def contract(name: str) -> dict[str, Any]:
    data = yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


class LiveApi:
    """The control plane served over real HTTP by uvicorn in a background thread, for clients
    that bring their own transport (the SDK, the console's end-to-end tests)."""

    def __init__(
        self, settings: ControlPlaneSettings, subjects: dict[str, Role], **app_kwargs: Any
    ) -> None:
        import socket
        import threading

        import uvicorn

        self.organization_id, self.token = asyncio.run(
            _bootstrap(str(settings.database_url), subjects)
        )
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(
            create_app(settings, **app_kwargs),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("the control plane did not start")
            time.sleep(0.05)

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)


@pytest.fixture
def live_api(settings: ControlPlaneSettings) -> Iterator[LiveApi]:
    api = LiveApi(settings, {})
    yield api
    api.close()


LiveApiFactory = Callable[..., LiveApi]


@pytest.fixture
def make_live_api(settings: ControlPlaneSettings) -> Iterator[LiveApiFactory]:
    """``make_live_api(settings_overrides={...}, memberships={...}, oidc_verifier=...)``."""
    apis: list[LiveApi] = []

    def factory(**app_kwargs: Any) -> LiveApi:
        overrides = app_kwargs.pop("settings_overrides", {})
        subjects = app_kwargs.pop("memberships", {})
        api = LiveApi(settings.model_copy(update=overrides), subjects, **app_kwargs)
        apis.append(api)
        return api

    yield factory
    for api in apis:
        api.close()
