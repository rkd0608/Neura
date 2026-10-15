"""Request authentication: API tokens and OIDC access tokens (design §20.7)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import ReasonCode
from neura_control.api.common import transaction
from neura_control.clock import utcnow
from neura_control.db.models import ApiToken, Membership
from neura_control.problems import ProblemError
from neura_control.security.oidc import OidcVerifier, TokenInvalidError
from neura_control.security.principal import (
    ROLE_PERMISSIONS,
    SCOPE_PERMISSIONS,
    Permission,
    Principal,
    PrincipalKind,
    Role,
)
from neura_control.security.tokens import API_TOKEN_PREFIX, parse_token, verify_secret
from neura_control.services import records

# Avoid a write on every request: record token use at most this often.
LAST_USED_RESOLUTION = timedelta(minutes=1)


async def authenticate(request: Request) -> Principal:
    header = request.headers.get("Authorization", "")
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        raise ProblemError(ReasonCode.API_UNAUTHENTICATED)
    credential = credential.strip()
    if credential.startswith(f"{API_TOKEN_PREFIX}_"):
        principal = await _api_token_principal(request, credential)
    else:
        principal = await _oidc_principal(request, credential)
    request.state.principal = principal
    return principal


async def _api_token_principal(request: Request, credential: str) -> Principal:
    parsed = parse_token(credential)
    if parsed is None or parsed.prefix != API_TOKEN_PREFIX:
        raise ProblemError(ReasonCode.API_UNAUTHENTICATED)
    now = utcnow()
    async with transaction(request) as session:
        token = await session.get(ApiToken, parsed.resource_id)
        if (
            token is None
            or token.revoked_at is not None
            or token.expires_at <= now
            or not verify_secret(parsed.secret, token.secret_hash)
        ):
            raise ProblemError(ReasonCode.API_UNAUTHENTICATED)
        if token.last_used_at is None or now - token.last_used_at > LAST_USED_RESOLUTION:
            token.last_used_at = now
        permissions: frozenset[Permission] = frozenset().union(
            *(SCOPE_PERMISSIONS[s] for s in token.scopes if s in SCOPE_PERMISSIONS)
        )
        return Principal(
            subject=f"api_token:{token.id}",
            kind=PrincipalKind.API_TOKEN,
            grants={token.organization_id: permissions},
            project_ids=frozenset(token.project_ids) if token.project_ids else None,
        )


async def _oidc_principal(request: Request, credential: str) -> Principal:
    verifier: OidcVerifier | None = request.app.state.oidc
    if verifier is None:
        raise ProblemError(ReasonCode.API_UNAUTHENTICATED)
    try:
        # In a thread: a key-cache miss fetches the identity provider's keys over the network.
        identity = await asyncio.to_thread(verifier.identity, credential)
    except TokenInvalidError as exc:
        raise ProblemError(ReasonCode.API_UNAUTHENTICATED) from exc
    async with transaction(request) as session:
        if identity.email is not None:
            await _accept_invitations(session, identity.subject, identity.email)
        memberships = (
            await session.scalars(select(Membership).where(Membership.subject == identity.subject))
        ).all()
    grants = {m.organization_id: ROLE_PERMISSIONS[Role(m.role)] for m in memberships}
    return Principal(subject=identity.subject, kind=PrincipalKind.USER, grants=grants)


async def _accept_invitations(session: AsyncSession, subject: str, email: str) -> None:
    """Bind invitations for this verified email to the signed-in subject (first sign-in only).
    An organization where the subject is already a member keeps that membership."""
    invitations = (
        await session.scalars(
            select(Membership)
            .where(Membership.email == email, Membership.subject.is_(None))
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not invitations:
        return
    member_of = set(
        (
            await session.scalars(
                select(Membership.organization_id).where(Membership.subject == subject)
            )
        ).all()
    )
    for invitation in invitations:
        if invitation.organization_id in member_of:
            continue
        invitation.subject = subject
        invitation.version += 1
        invitation.updated_at = utcnow()
        await records.audit(
            session,
            principal=Principal(subject=subject, kind=PrincipalKind.USER, grants={}),
            organization_id=invitation.organization_id,
            project_id=None,
            action="member.accept",
            target=invitation.id,
            request_id="sign-in",
        )
