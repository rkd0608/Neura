"""Control-plane HTTP application."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import PostgresDsn
from sqlalchemy import delete
from starlette.exceptions import HTTPException as StarletteHTTPException

from neura.v1 import common_pb2
from neura_contracts import API_VERSION as CONTRACTS_API_VERSION
from neura_contracts import ReasonCode
from neura_control.api import (
    routes_access,
    routes_contracts,
    routes_core,
    routes_deployments,
    routes_improvement,
    routes_observe,
    routes_weights,
)
from neura_control.clock import utcnow
from neura_control.database import database_reachable
from neura_control.db.engine import create_engine, session_factory
from neura_control.db.models import EnrollmentToken, Event, IdempotencyKey
from neura_control.grpc_site import start_agent_server
from neura_control.metrics import ControlPlaneMetrics, route_template
from neura_control.problems import (
    ProblemError,
    handle_http_exception,
    handle_problem,
    handle_unexpected,
    handle_validation_error,
    problem_response,
)
from neura_control.security.oidc import OidcVerifier
from neura_control.security.signing import ManifestSigner
from neura_control.services import bootstrap
from neura_control.services.sealed_weights import SealedWeightsStore
from neura_control.settings import ControlPlaneSettings

SERVICE_NAME = "neura-control"
API_VERSION = "v1"
IDEMPOTENCY_RETENTION = timedelta(hours=24)

log = logging.getLogger("neura.control")
DatabaseProbe = Callable[[PostgresDsn, float], Awaitable[bool]]


def _package_version() -> str:
    try:
        return version("neura-control")
    except PackageNotFoundError:  # pragma: no cover - only when run from an unbuilt tree
        return "0.0.0+unknown"


class RateLimiter:
    """Token buckets by key. Per process; a shared store is needed with many replicas.

    Memory is bounded by evicting the least recently used bucket. An evicted bucket had been idle
    longest, so it had refilled anyway: eviction never resets a limit someone is running into.
    """

    def __init__(self, per_minute: int, *, max_keys: int = 100_000) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self.max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def take(self, key: str, now: float) -> float | None:
        """Consume one token; return None if allowed, else seconds to wait."""
        tokens, last = self._buckets.pop(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            wait: float | None = (1.0 - tokens) / self.rate
        else:
            self._buckets[key] = (tokens - 1.0, now)
            wait = None
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)
        return wait


async def _prune(app: FastAPI, settings: ControlPlaneSettings, stop: asyncio.Event) -> None:
    """Drop events past retention, old idempotency records and expired enrollment tokens.
    Stopped with ``stop`` rather than cancelled, so a transaction in progress completes."""
    while True:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=60)
        if stop.is_set():
            return
        with contextlib.suppress(Exception):
            async with app.state.sessions.begin() as session:
                now = utcnow()
                await session.execute(
                    delete(Event).where(
                        Event.occurred_at < now - timedelta(hours=settings.event_retention_hours)
                    )
                )
                await session.execute(
                    delete(IdempotencyKey).where(
                        IdempotencyKey.created_at < now - IDEMPOTENCY_RETENTION
                    )
                )
                await session.execute(
                    delete(EnrollmentToken).where(
                        EnrollmentToken.expires_at < now - timedelta(days=1)
                    )
                )


async def _dev_bootstrap(app: FastAPI, settings: ControlPlaneSettings) -> None:
    assert settings.dev_bootstrap_token is not None
    async with app.state.sessions.begin() as session:
        org = await bootstrap.ensure_organization(session, "dev")
        await bootstrap.create_api_token(
            session,
            org.id,
            display_name="development bootstrap",
            scopes=["admin"],
            expires_in_days=365,
            token=settings.dev_bootstrap_token,
        )
    log.warning("development bootstrap: organization %s with an admin token is ready", org.id)


def iter_routes(app: FastAPI) -> Iterator[tuple[str, str]]:
    """Yield (method, path) for every route, including those of included routers."""

    def walk(routes: Any, prefix: str) -> Iterator[tuple[str, str]]:
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                context = getattr(route, "include_context", None)
                yield from walk(included.routes, prefix + str(getattr(context, "prefix", "") or ""))
                continue
            path = getattr(route, "path", None)
            for method in getattr(route, "methods", None) or ():
                if path is not None and method != "HEAD":
                    yield method, prefix + path

    return walk(app.routes, "")


def create_app(
    settings: ControlPlaneSettings | None = None,
    *,
    database_probe: DatabaseProbe = database_reachable,
    oidc_verifier: OidcVerifier | None = None,
) -> FastAPI:
    settings = settings or ControlPlaneSettings()
    spec_text = settings.openapi_path.read_text(encoding="utf-8")
    spec_version = str(yaml.safe_load(spec_text)["info"]["version"])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(str(settings.database_url), pool_size=settings.database_pool_size)
        app.state.engine = engine
        app.state.sessions = session_factory(engine)
        if oidc_verifier is not None:
            app.state.oidc = oidc_verifier
        elif settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_url:
            app.state.oidc = OidcVerifier(
                issuer=settings.oidc_issuer,
                audience=settings.oidc_audience,
                jwks_url=settings.oidc_jwks_url,
                allowed_clients=frozenset(settings.oidc_allowed_clients),
                required_amr=frozenset(settings.oidc_required_amr),
            )
        else:
            app.state.oidc = None
        if settings.dev_bootstrap_token:
            await _dev_bootstrap(app, settings)
        signer = ManifestSigner(settings.manifest_signing_key)
        app.state.signer = signer
        metrics_server = None
        if settings.metrics_listen:
            metrics_server, metrics_thread = metrics.serve(settings.metrics_listen)
        agent_server = None
        if settings.agent_grpc_listen:
            agent_server, app.state.site_control, app.state.agent_port = await start_agent_server(
                app.state.sessions,
                signer,
                settings,
                weights=app.state.sealed_weights,
                metrics=metrics,
            )
        stop_pruning = asyncio.Event()
        pruner = asyncio.create_task(_prune(app, settings, stop_pruning))
        try:
            yield
        finally:
            if agent_server is not None:
                await agent_server.stop(grace=1.0)
                await app.state.site_control.close()
            stop_pruning.set()
            await pruner
            await engine.dispose()
            if metrics_server is not None:
                metrics_server.shutdown()
                metrics_server.server_close()
                metrics_thread.join(timeout=5)

    app = FastAPI(
        title="Neura control plane",
        version=_package_version(),
        lifespan=lifespan,
        # The published contract is api/openapi.yaml (spec-first, design §20.1); FastAPI's
        # generated document and interactive docs are disabled so there is only one source.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.event_streams = [0]  # open change-event streams (routes_observe)
    app.state.oidc = oidc_verifier
    metrics = ControlPlaneMetrics()
    app.state.metrics = metrics
    app.state.sealed_weights = (
        SealedWeightsStore(settings.sealed_weights_dir, settings.sealed_weights_max_bytes)
        if settings.sealed_weights_dir is not None
        else None
    )
    limiter = RateLimiter(settings.rate_limit_per_minute)
    address_limiter = RateLimiter(settings.rate_limit_per_address_per_minute)

    app.add_exception_handler(ProblemError, handle_problem)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("X-Request-Id", "")
        request_id = (
            incoming
            if 0 < len(incoming) <= 64 and incoming.isprintable()
            else secrets.token_hex(12)
        )
        request.state.request_id = request_id
        if request.url.path.startswith("/v1/") and request.url.path != "/v1/meta":
            now = time.monotonic()
            address = request.client.host if request.client else ""
            wait = address_limiter.take(address, now)
            credential = request.headers.get("Authorization")
            if wait is None and credential:
                wait = limiter.take(hashlib.sha256(credential.encode()).hexdigest(), now)
            if wait is not None:
                response: Response = problem_response(
                    ReasonCode.API_RATE_LIMITED, instance=request.url.path
                )
                response.headers["Retry-After"] = str(max(1, round(wait)))
                response.headers["X-Request-Id"] = request_id
                return response
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        metrics.request(
            route_template(request.scope),
            request.method,
            response.status_code,
            time.perf_counter() - started,
        )
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness: the process is serving requests."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, Any]:
        """Readiness: dependencies needed to serve the API are reachable."""
        if not await database_probe(settings.database_url, settings.database_connect_timeout_s):
            raise ProblemError(ReasonCode.API_UNAVAILABLE, "database is not reachable")
        return {"status": "ready", "checks": {"database": "ok"}}

    @app.get("/v1/meta")
    async def meta() -> dict[str, Any]:
        return {
            "service": SERVICE_NAME,
            "service_version": app.version,
            "api_version": API_VERSION,
            "api_spec_version": spec_version,
            "contracts_api_version": CONTRACTS_API_VERSION,
            "protocol_version": common_pb2.ProtocolVersion.Name(common_pb2.PROTOCOL_VERSION_1),
            "environment": settings.environment,
            "authentication": {
                "api_tokens": True,
                "oidc": {
                    "issuer": settings.oidc_issuer,
                    "client_id": settings.console_oidc_client_id,
                    "audience": settings.oidc_audience,
                    "scopes": settings.console_oidc_scopes,
                }
                if settings.oidc_issuer and settings.console_oidc_client_id
                else None,
            },
        }

    @app.get("/v1/openapi.yaml", include_in_schema=False)
    async def openapi_document() -> Response:
        return Response(spec_text, media_type="application/yaml")

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse({"service": SERVICE_NAME, "meta": "/v1/meta"})

    for module in (
        routes_core,
        routes_contracts,
        routes_deployments,
        routes_improvement,
        routes_observe,
        routes_access,
        routes_weights,
    ):
        app.include_router(module.router)

    return app
