"""Creating the first organization, memberships and tokens (CLI and development bootstrap)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from neura_control.clock import utcnow
from neura_control.db.models import ApiToken, Membership, Organization
from neura_control.ids import Kind, new_id
from neura_control.security.principal import Role
from neura_control.security.tokens import (
    API_TOKEN_PREFIX,
    format_token,
    hash_secret,
    new_secret,
    parse_token,
)


async def ensure_organization(session: AsyncSession, display_name: str) -> Organization:
    existing = (
        await session.scalars(select(Organization).where(Organization.display_name == display_name))
    ).first()
    if existing is not None:
        return existing
    org = Organization(
        id=new_id(Kind.ORGANIZATION), display_name=display_name, created_by="bootstrap"
    )
    session.add(org)
    await session.flush()
    return org


async def add_membership(
    session: AsyncSession, organization_id: str, subject: str, role: Role
) -> None:
    await session.execute(
        insert(Membership)
        .values(
            id=new_id(Kind.MEMBER),
            organization_id=organization_id,
            subject=subject,
            role=role.value,
            version=1,
            created_by="bootstrap",
        )
        .on_conflict_do_update(
            constraint="uq_memberships_org_subject",
            set_={"role": role.value, "version": Membership.version + 1},
        )
    )


async def create_api_token(
    session: AsyncSession,
    organization_id: str,
    *,
    display_name: str,
    scopes: list[str],
    expires_in_days: int = 30,
    token: str | None = None,
) -> str:
    """Create a token and return its secret value. With ``token``, that exact value is used
    (development bootstrap only) and creation is idempotent."""
    if token is not None:
        parsed = parse_token(token)
        if parsed is None or parsed.prefix != API_TOKEN_PREFIX:
            raise ValueError(
                "the token is not a valid API token; generate one with `neura-control dev-token`"
            )
        if await session.get(ApiToken, parsed.resource_id) is not None:
            return token
        token_id, secret = parsed.resource_id, parsed.secret
    else:
        token_id, secret = new_id(Kind.API_TOKEN), new_secret()
    session.add(
        ApiToken(
            id=token_id,
            organization_id=organization_id,
            display_name=display_name,
            scopes=sorted(set(scopes)),
            project_ids=[],
            secret_hash=hash_secret(secret),
            expires_at=utcnow() + timedelta(days=expires_in_days),
            created_by="bootstrap",
        )
    )
    await session.flush()
    return format_token(API_TOKEN_PREFIX, token_id, secret)


def generate_token_value() -> str:
    return format_token(API_TOKEN_PREFIX, new_id(Kind.API_TOKEN), new_secret())
