"""Local site API served by the agent (design §19.5).

Available without the internet: operators check status, approve or refuse activations and issue
robot credentials here; robots fetch the revision they should run. Mutations need the local operator
credential. Robots do not use this API: they reach only the gateway, with their certificates.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from neura.v1 import common_pb2
from neura_agent.connectivity import ControlPlaneProbe
from neura_agent.core import LocalActionError
from neura_agent.runner import AgentRuntime
from neura_agent.settings import AgentSettings
from neura_contracts import REASON_CODES, ReasonCode

SERVICE_NAME = "neura-agent"


def _package_version() -> str:
    try:
        return version("neura-agent")
    except PackageNotFoundError:  # pragma: no cover
        return "0.0.0+unknown"


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=2000)


class CertificateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csr: str = Field(min_length=1, max_length=16_000)


class LocalError(Exception):
    def __init__(self, code: ReasonCode, status: int, detail: str = "") -> None:
        super().__init__(detail)
        self.code, self.status, self.detail = code, status, detail


def _bearer(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    return header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""


def create_app(
    settings: AgentSettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    start_probe: bool = True,
    runtime: AgentRuntime | None = None,
    start_runtime: bool = False,
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
        if runtime is not None and start_runtime:
            await runtime.start()
        try:
            yield
        finally:
            if runtime is not None and start_runtime:
                await runtime.stop()
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
    app.state.runtime = runtime

    @app.exception_handler(LocalError)
    async def local_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, LocalError)
        info = REASON_CODES[exc.code]
        return JSONResponse(
            {"reason": exc.code.value, "title": info.message, "detail": exc.detail},
            status_code=exc.status,
            media_type="application/problem+json",
        )

    def core() -> Any:
        if runtime is None or runtime.core is None:
            raise LocalError(ReasonCode.API_UNAVAILABLE, 503, "the agent is not enrolled yet")
        return runtime.core

    def operator(request: Request) -> None:
        expected = settings.operator_token_sha256
        token = _bearer(request)
        if expected is None:
            raise LocalError(
                ReasonCode.API_FORBIDDEN, 403, "local approvals are disabled on this server"
            )
        if not token or not hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), expected
        ):
            raise LocalError(ReasonCode.API_UNAUTHENTICATED, 401, "operator credential required")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/local/status")
    async def status() -> dict[str, Any]:
        snapshot = asdict(probe.snapshot())
        if snapshot["reason"] is not None:
            snapshot["reason"] = str(snapshot["reason"])
        body: dict[str, Any] = {
            "service": SERVICE_NAME,
            "agent_version": app.version,
            "protocol_version": common_pb2.ProtocolVersion.Name(common_pb2.PROTOCOL_VERSION_1),
            "enrolled": False,
            "control_plane": snapshot,
        }
        identity = runtime.identities.load() if runtime is not None else None
        if identity is not None:
            body["enrolled"] = True
            body["identity"] = {"server_id": identity.server_id, "site_id": identity.site_id}
        if runtime is not None and runtime.core is not None:
            c = runtime.core
            remaining = c.offline_remaining_s()
            body["connection"] = {
                "connected": bool(runtime.connection and runtime.connection.connected),
                "last_error": runtime.connection.last_error if runtime.connection else None,
            }
            body["offline_authorization"] = {
                "remaining_s": None if remaining is None else round(remaining),
                "expired": c.authorization_expired(),
            }
            body["site"] = c.diagnostics()
            body["pending_approvals"] = [asdict(a) for a in c.pending_approvals()]
        return body

    @app.get("/v1/local/approvals")
    async def approvals() -> dict[str, Any]:
        return {"items": [asdict(a) for a in core().pending_approvals()]}

    @app.post("/v1/local/approvals/{command_id}/approve")
    async def approve(command_id: str, decision: Decision, request: Request) -> dict[str, Any]:
        operator(request)
        try:
            activation = await core().approve(command_id, decision.operator)
        except LocalActionError as exc:
            raise LocalError(
                exc.code, 404 if exc.code is ReasonCode.API_NOT_FOUND else 409, str(exc)
            ) from exc
        return asdict(activation)

    @app.post("/v1/local/approvals/{command_id}/refuse")
    async def refuse(command_id: str, decision: Decision, request: Request) -> dict[str, str]:
        operator(request)
        try:
            await core().refuse(command_id, decision.operator, decision.note)
        except LocalActionError as exc:
            raise LocalError(exc.code, 404, str(exc)) from exc
        return {"status": "refused"}

    @app.post("/v1/local/robots/{name}/certificate")
    async def issue_certificate(
        name: str, body: CertificateRequest, request: Request
    ) -> dict[str, str]:
        """Sign a robot's CSR with the site CA (design §17.3); the robot's key stays on it."""
        operator(request)
        c = core()
        try:
            certificate, ca = c.issue_robot_certificate(name, body.csr.encode())
        except LocalActionError as exc:
            status = 404 if exc.code is ReasonCode.API_NOT_FOUND else 422
            raise LocalError(exc.code, status, str(exc)) from exc
        await c.reconcile()
        return {"robot": name, "certificate": certificate.decode(), "ca": ca.decode()}

    @app.delete("/v1/local/robots/{name}/certificate")
    async def revoke_certificates(name: str, request: Request) -> dict[str, Any]:
        operator(request)
        c = core()
        revoked = c.revoke_robot_certificates(name)
        await c.reconcile()
        return {"robot": name, "revoked": revoked}

    @app.post("/v1/local/robots/{name}/credentials")
    async def issue_credentials(name: str, request: Request) -> dict[str, str]:
        """A device token, for a development gateway without TLS only."""
        operator(request)
        c = core()
        if settings.gateway_tls:
            raise LocalError(
                ReasonCode.API_CONFLICT,
                409,
                "the gateway uses mutual TLS: issue a certificate (neura-agent robot-certificate)",
            )
        if name not in {r["name"] for r in c.manifest().get("robots", [])}:
            raise LocalError(ReasonCode.API_NOT_FOUND, 404, "no such robot at this site")
        token = f"nrt_{secrets.token_urlsafe(32)}"
        c.state.robot_credentials[name] = hashlib.sha256(token.encode()).hexdigest()
        c.store.save(c.state)
        c.audit.write("robot_credentials_issued", robot=name)
        await c.reconcile()
        return {"robot": name, "token": token}

    @app.get("/v1/local/robots/{name}/assignment")
    async def assignment(name: str, request: Request) -> dict[str, Any]:
        """What a robot is assigned (robots ask the gateway; this is for the operator)."""
        operator(request)
        found = core().assignment_for(name)
        if found is None:
            raise LocalError(
                ReasonCode.DEPLOYMENT_NOT_ACTIVE, 404, "no revision is assigned to this robot"
            )
        result: dict[str, Any] = found
        return result

    @app.get("/v1/local/audit")
    async def audit(request: Request) -> dict[str, Any]:
        operator(request)
        return {"items": core().audit.tail()}

    return app
