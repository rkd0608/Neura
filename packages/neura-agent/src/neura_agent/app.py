"""Local site API served by the agent (design §19.5)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx
from fastapi import FastAPI

from neura.v1 import common_pb2
from neura_agent.connectivity import ControlPlaneProbe
from neura_agent.settings import AgentSettings

SERVICE_NAME = "neura-agent"


def _package_version() -> str:
    try:
        return version("neura-agent")
    except PackageNotFoundError:  # pragma: no cover
        return "0.0.0+unknown"


def create_app(
    settings: AgentSettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    start_probe: bool = True,
) -> FastAPI:
    settings = settings or AgentSettings()
    client = httpx.AsyncClient(
        timeout=settings.probe_timeout_s,
        transport=transport,
        headers={"user-agent": f"{SERVICE_NAME}/{_package_version()}"},
    )
    probe = ControlPlaneProbe(
        client, str(settings.control_plane_url), interval_s=settings.probe_interval_s
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_probe:
            probe.start()
        try:
            yield
        finally:
            await probe.stop()
            await client.aclose()

    app = FastAPI(
        title="Neura site agent (local API)",
        version=_package_version(),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.probe = probe

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/local/status")
    async def status() -> dict[str, Any]:
        snapshot = asdict(probe.snapshot())
        if snapshot["reason"] is not None:
            snapshot["reason"] = str(snapshot["reason"])
        return {
            "service": SERVICE_NAME,
            "agent_version": app.version,
            "protocol_version": common_pb2.ProtocolVersion.Name(common_pb2.PROTOCOL_VERSION_1),
            "enrolled": False,
            "control_plane": snapshot,
        }

    return app
