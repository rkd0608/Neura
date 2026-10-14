"""Control-plane HTTP application."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import PostgresDsn
from starlette.exceptions import HTTPException as StarletteHTTPException

from neura.v1 import common_pb2
from neura_contracts import API_VERSION as CONTRACTS_API_VERSION
from neura_contracts import ReasonCode
from neura_control.database import database_reachable
from neura_control.problems import (
    ProblemError,
    handle_http_exception,
    handle_problem,
    handle_unexpected,
    handle_validation_error,
)
from neura_control.settings import ControlPlaneSettings

SERVICE_NAME = "neura-control"
API_VERSION = "v1"

DatabaseProbe = Callable[[PostgresDsn, float], Awaitable[bool]]


def _package_version() -> str:
    try:
        return version("neura-control")
    except PackageNotFoundError:  # pragma: no cover - only when run from an unbuilt tree
        return "0.0.0+unknown"


def create_app(
    settings: ControlPlaneSettings | None = None,
    *,
    database_probe: DatabaseProbe = database_reachable,
) -> FastAPI:
    settings = settings or ControlPlaneSettings()
    app = FastAPI(
        title="Neura control plane",
        version=_package_version(),
        # The published contract is api/openapi.yaml (spec-first, design §20.1); FastAPI's
        # generated document and interactive docs are disabled so there is only one source.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings

    app.add_exception_handler(ProblemError, handle_problem)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected)

    spec_text = settings.openapi_path.read_text(encoding="utf-8")
    spec_version = str(yaml.safe_load(spec_text)["info"]["version"])

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
        }

    @app.get("/v1/openapi.yaml", include_in_schema=False)
    async def openapi_document() -> Response:
        return Response(spec_text, media_type="application/yaml")

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse({"service": SERVICE_NAME, "meta": "/v1/meta"})

    return app
