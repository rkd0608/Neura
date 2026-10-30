"""Signing the CLI in through the browser (device authorization, after RFC 8628).

``neura login`` starts a login session and shows a short code; the person opens the console,
signs in as usual (their SSO, or an API token) and approves the code. The CLI then exchanges the
session's secret for an API token with that person's permissions in the workspace they chose. The
CLI never sees the person's credentials, and a code alone is worth nothing without the secret.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import ReasonCode
from neura_control.api.auth import authenticate
from neura_control.api.common import ApiResult, idempotent, parse_body, transaction
from neura_control.api.schemas import LoginApprovalCreate, LoginSessionCreate, LoginTokenRequest
from neura_control.api.scope import Ctx
from neura_control.clock import iso, utcnow
from neura_control.db.models import ApiToken, LoginSession
from neura_control.ids import Kind, new_id
from neura_control.problems import ProblemError
from neura_control.security.principal import Permission, Principal, PrincipalKind
from neura_control.security.tokens import (
    API_TOKEN_PREFIX,
    format_token,
    hash_secret,
    new_secret,
    verify_secret,
)
from neura_control.services import records, workspace
from neura_control.settings import ControlPlaneSettings

router = APIRouter()

ANONYMOUS = Principal(subject="anonymous:login", kind=PrincipalKind.SYSTEM)
LOGIN_TTL = timedelta(minutes=10)
TOKEN_TTL = timedelta(days=30)
POLL_INTERVAL_S = 2
# No vowels and no look-alikes (0/O, 1/I): codes are read off one screen and typed on another.
_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"


def _user_code() -> str:
    code = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{code[:4]}-{code[4:]}"


def _normalized(code: str) -> str:
    plain = code.replace("-", "").upper()
    return f"{plain[:4]}-{plain[4:]}"


@router.post("/v1/login-sessions", status_code=201)
async def create_login_session(request: Request) -> Response:
    body = await parse_body(request, LoginSessionCreate)
    settings: ControlPlaneSettings = request.app.state.settings
    console = (settings.console_url or settings.public_url).rstrip("/")

    async def work(session: AsyncSession) -> ApiResult:
        secret = new_secret()
        row = LoginSession(
            id=new_id(Kind.LOGIN_SESSION),
            user_code=_user_code(),
            secret_hash=hash_secret(secret),
            client_name=body.client_name,
            state="pending",
            expires_at=utcnow() + LOGIN_TTL,
        )
        session.add(row)
        await session.flush()
        return ApiResult(
            201,
            {
                "id": row.id,
                "user_code": row.user_code,
                "verification_url": f"{console}/activate?code={row.user_code}",
                "secret": secret,
                "expires_at": iso(row.expires_at),
                "interval_s": POLL_INTERVAL_S,
            },
            {"Location": f"/v1/login-sessions/{row.id}"},
        )

    return await idempotent(request, ANONYMOUS, work)


@router.post("/v1/login-sessions/{login_session_id}/token")
async def exchange_login_session(login_session_id: str, request: Request) -> Response:
    body = await parse_body(request, LoginTokenRequest)

    async def work(session: AsyncSession) -> ApiResult:
        row = await session.get(LoginSession, login_session_id, with_for_update=True)
        if row is None or not verify_secret(body.secret, row.secret_hash):
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        if row.state == "consumed" or row.expires_at < utcnow():
            raise ProblemError(
                ReasonCode.API_NOT_FOUND, "this sign-in has expired: run neura login again"
            )
        if row.state != "approved" or row.organization_id is None:
            raise ProblemError(
                ReasonCode.API_LOGIN_PENDING,
                f"open the link and approve code {row.user_code}",
            )
        token_id = new_id(Kind.API_TOKEN)
        secret = new_secret()
        expires_at = utcnow() + TOKEN_TTL
        session.add(
            ApiToken(
                id=token_id,
                organization_id=row.organization_id,
                display_name=f"CLI: {row.client_name}"[:200],
                scopes=row.scopes,
                project_ids=[],
                secret_hash=hash_secret(secret),
                expires_at=expires_at,
                created_by=row.approved_by or "unknown",
            )
        )
        row.state, row.api_token_id = "consumed", token_id
        return ApiResult(
            200,
            {
                "token": format_token(API_TOKEN_PREFIX, token_id, secret),
                "organization_id": row.organization_id,
                "scopes": row.scopes,
                "expires_at": iso(expires_at),
            },
        )

    return await idempotent(request, ANONYMOUS, work)


@router.get("/v1/login-codes/{user_code}")
async def get_login_code(
    user_code: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    """What waits for a code, shown before anyone approves it: anyone can start a sign-in and
    send its link, so the person sees what started it and when. As only people approve codes,
    only people look them up."""
    if principal.kind is not PrincipalKind.USER:
        raise ProblemError(
            ReasonCode.API_FORBIDDEN, "only a signed-in person can approve a sign-in"
        )
    async with transaction(request) as session:
        row = (
            await session.scalars(
                select(LoginSession).where(LoginSession.user_code == _normalized(user_code))
            )
        ).first()
        if row is None or row.state != "pending" or row.expires_at < utcnow():
            raise ProblemError(
                ReasonCode.API_NOT_FOUND, "no sign-in is waiting for this code; it may have expired"
            )
        return ApiResult(
            200,
            {
                "user_code": row.user_code,
                "client_name": row.client_name,
                "created_at": iso(row.created_at),
                "expires_at": iso(row.expires_at),
            },
        ).response()


@router.post("/v1/login-approvals")
async def approve_login(request: Request, principal: Principal = Depends(authenticate)) -> Response:
    """A signed-in person approves a CLI's code for one of their workspaces. The CLI receives
    a token with that person's permissions there, for 30 days."""
    body = await parse_body(request, LoginApprovalCreate)
    if principal.kind is not PrincipalKind.USER:
        raise ProblemError(
            ReasonCode.API_FORBIDDEN, "only a signed-in person can approve a sign-in"
        )
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        org = await workspace.organization(session, principal, body.organization_id)
        row = (
            await session.scalars(
                select(LoginSession)
                .where(LoginSession.user_code == _normalized(body.user_code))
                .with_for_update()
            )
        ).first()
        if row is None or row.state != "pending" or row.expires_at < utcnow():
            raise ProblemError(
                ReasonCode.API_NOT_FOUND, "no sign-in is waiting for this code; it may have expired"
            )
        granted = principal.permissions(org.id)
        scopes = [
            scope
            for scope, permission in (
                ("admin", Permission.ADMIN),
                ("deploy", Permission.DEPLOY),
                ("read", Permission.READ),
            )
            if permission in granted
        ][:1]
        row.state, row.organization_id = "approved", org.id
        row.approved_by, row.scopes = principal.subject, scopes
        await records.audit(
            session,
            principal=principal,
            organization_id=org.id,
            project_id=None,
            action="login.approve",
            target=row.id,
            request_id=ctx.request_id,
        )
        return ApiResult(
            200,
            {"client_name": row.client_name, "organization_id": org.id, "scopes": scopes},
        )

    return await idempotent(request, principal, work)
