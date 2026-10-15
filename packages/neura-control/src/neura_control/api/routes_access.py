"""Members, API tokens and support access grants (design §18.2, §19.2, §20.7)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import ReasonCode
from neura_control.api import serializers
from neura_control.api.auth import authenticate
from neura_control.api.common import (
    ApiResult,
    check_version,
    idempotent,
    if_match,
    page_query,
    paginate,
    parse_body,
    resource_result,
    transaction,
)
from neura_control.api.schemas import ApiTokenCreate, MemberCreate, MemberUpdate, SupportGrantCreate
from neura_control.api.scope import Ctx, touch
from neura_control.clock import utcnow
from neura_control.db.models import (
    ApiToken,
    Membership,
    Organization,
    Project,
    Site,
    SupportGrant,
)
from neura_control.ids import Kind, new_id
from neura_control.problems import ProblemError
from neura_control.security.principal import Permission, Principal
from neura_control.security.tokens import API_TOKEN_PREFIX, format_token, hash_secret, new_secret
from neura_control.services import records

router = APIRouter()


async def _audit_org(
    session: AsyncSession, ctx: Ctx, organization_id: str, action: str, target: str
) -> None:
    await records.audit(
        session,
        principal=ctx.principal,
        organization_id=organization_id,
        project_id=None,
        action=action,
        target=target,
        request_id=ctx.request_id,
    )


@router.get("/v1/me")
async def me(request: Request, principal: Principal = Depends(authenticate)) -> Response:
    async with transaction(request) as session:
        orgs = (
            await session.scalars(
                select(Organization).where(Organization.id.in_(list(principal.grants)))
            )
        ).all()
    return ApiResult(
        200,
        {
            "subject": principal.subject,
            "kind": principal.kind.value,
            "organizations": [
                {
                    "id": org.id,
                    "display_name": org.display_name,
                    "permissions": sorted(p.value for p in principal.grants[org.id]),
                }
                for org in sorted(orgs, key=lambda o: o.id)
            ],
            "project_ids": sorted(principal.project_ids)
            if principal.project_ids is not None
            else None,
        },
    ).response()


# --- members ---------------------------------------------------------------------------------


@router.get("/v1/organizations/{organization_id}/members")
async def list_members(
    organization_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).organization(session, organization_id, Permission.ADMIN)
        page = await paginate(
            session,
            select(Membership).where(Membership.organization_id == organization_id),
            Membership.id,
            query,
            serializers.member,
        )
    return ApiResult(200, page).response()


@router.post("/v1/organizations/{organization_id}/members", status_code=201)
async def add_member(
    organization_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, MemberCreate)
    ctx = Ctx(request, principal)
    email = body.email.lower() if body.email else None

    async def work(session: AsyncSession) -> ApiResult:
        await ctx.organization(session, organization_id, Permission.ADMIN)
        existing = (
            await session.scalars(
                select(Membership).where(
                    Membership.organization_id == organization_id,
                    Membership.email == email
                    if email is not None
                    else Membership.subject == body.subject,
                )
            )
        ).first()
        if existing is not None:
            raise ProblemError(
                ReasonCode.API_CONFLICT,
                f"{email or body.subject} is already a member ({existing.id})",
            )
        member = Membership(
            id=new_id(Kind.MEMBER),
            organization_id=organization_id,
            email=email,
            subject=body.subject,
            role=body.role,
            created_by=principal.subject,
        )
        session.add(member)
        await session.flush()
        await _audit_org(session, ctx, organization_id, "member.add", member.id)
        await session.refresh(member)
        return resource_result(
            201, serializers.member(member), 1, f"/v1/organizations/{organization_id}/members"
        )

    return await idempotent(request, principal, work)


async def _ensure_an_admin_remains(session: AsyncSession, member: Membership) -> None:
    """An organization must keep at least one active admin, or nobody could manage it."""
    if member.role != "admin" or member.subject is None:
        return
    others = await session.scalar(
        select(func.count())
        .select_from(Membership)
        .where(
            Membership.organization_id == member.organization_id,
            Membership.role == "admin",
            Membership.subject.is_not(None),
            Membership.id != member.id,
        )
    )
    if not others:
        raise ProblemError(
            ReasonCode.API_CONFLICT, "the organization would be left without an active admin"
        )


@router.patch("/v1/members/{member_id}")
async def update_member(
    member_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    body = await parse_body(request, MemberUpdate, merge_patch=True)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        member = await session.get(Membership, member_id, with_for_update=True)
        if member is None:
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        await ctx.organization(session, member.organization_id, Permission.ADMIN)
        check_version(member.version, expected)
        if body.role is not None and body.role != member.role:
            if body.role != "admin":
                await _ensure_an_admin_remains(session, member)
            member.role = body.role
            touch(member)
            await _audit_org(session, ctx, member.organization_id, "member.update", member.id)
        return resource_result(200, serializers.member(member), member.version).response()


@router.delete("/v1/members/{member_id}", status_code=204)
async def remove_member(
    member_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        member = await session.get(Membership, member_id, with_for_update=True)
        if member is None:
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        await ctx.organization(session, member.organization_id, Permission.ADMIN)
        check_version(member.version, expected)
        await _ensure_an_admin_remains(session, member)
        await session.delete(member)
        await _audit_org(session, ctx, member.organization_id, "member.remove", member.id)
    return Response(status_code=204)


# --- API tokens ------------------------------------------------------------------------------


@router.get("/v1/organizations/{organization_id}/api-tokens")
async def list_tokens(
    organization_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).organization(session, organization_id, Permission.ADMIN)
        statement = select(ApiToken).where(
            ApiToken.organization_id == organization_id, ApiToken.revoked_at.is_(None)
        )
        page = await paginate(session, statement, ApiToken.id, query, serializers.api_token)
    return ApiResult(200, page).response()


@router.post("/v1/organizations/{organization_id}/api-tokens", status_code=201)
async def create_token(
    organization_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, ApiTokenCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        await ctx.organization(session, organization_id, Permission.ADMIN)
        if body.project_ids:
            found = set(
                (
                    await session.scalars(
                        select(Project.id).where(
                            Project.id.in_(body.project_ids),
                            Project.organization_id == organization_id,
                        )
                    )
                ).all()
            )
            missing = sorted(set(body.project_ids) - found)
            if missing:
                raise ProblemError(ReasonCode.API_VALIDATION_FAILED, f"unknown projects {missing}")
        token_id = new_id(Kind.API_TOKEN)
        secret = new_secret()
        token = ApiToken(
            id=token_id,
            organization_id=organization_id,
            display_name=body.display_name,
            scopes=sorted(set(body.scopes)),
            project_ids=list(dict.fromkeys(body.project_ids)),
            secret_hash=hash_secret(secret),
            expires_at=utcnow() + timedelta(days=body.expires_in_days),
            created_by=principal.subject,
        )
        session.add(token)
        await session.flush()
        await _audit_org(session, ctx, organization_id, "api_token.create", token_id)
        await session.refresh(token)
        data = serializers.api_token(token)
        data["secret"] = format_token(API_TOKEN_PREFIX, token_id, secret)
        return resource_result(201, data, 1, f"/v1/organizations/{organization_id}/api-tokens")

    return await idempotent(request, principal, work)


@router.delete("/v1/api-tokens/{api_token_id}", status_code=204)
async def revoke_token(
    api_token_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        token = await session.get(ApiToken, api_token_id, with_for_update=True)
        if token is None or token.revoked_at is not None:
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        await ctx.organization(session, token.organization_id, Permission.ADMIN)
        check_version(token.version, expected)
        token.revoked_at = utcnow()
        touch(token)
        await _audit_org(session, ctx, token.organization_id, "api_token.revoke", token.id)
    return Response(status_code=204)


@router.get("/v1/organizations/{organization_id}/support-grants")
async def list_grants(
    organization_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).organization(session, organization_id, Permission.ADMIN)
        page = await paginate(
            session,
            select(SupportGrant).where(SupportGrant.organization_id == organization_id),
            SupportGrant.id,
            query,
            serializers.support_grant,
        )
    return ApiResult(200, page).response()


@router.post("/v1/organizations/{organization_id}/support-grants", status_code=201)
async def create_grant(
    organization_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, SupportGrantCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        await ctx.organization(session, organization_id, Permission.ADMIN)
        site = await session.get(Site, body.site_id)
        project = await session.get(Project, site.project_id) if site else None
        if site is None or project is None or project.organization_id != organization_id:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED, "site_id is not a site in this organization"
            )
        grant = SupportGrant(
            id=new_id(Kind.SUPPORT_GRANT),
            organization_id=organization_id,
            site_id=site.id,
            operations=sorted(set(body.operations)),
            justification=body.justification,
            expires_at=utcnow() + timedelta(minutes=body.duration_minutes),
            created_by=principal.subject,
        )
        session.add(grant)
        await session.flush()
        await _audit_org(session, ctx, organization_id, "support_grant.create", grant.id)
        await session.refresh(grant)
        return resource_result(
            201,
            serializers.support_grant(grant),
            1,
            f"/v1/organizations/{organization_id}/support-grants",
        )

    return await idempotent(request, principal, work)


@router.delete("/v1/support-grants/{support_grant_id}", status_code=204)
async def revoke_grant(
    support_grant_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        grant = await session.get(SupportGrant, support_grant_id, with_for_update=True)
        if grant is None:
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        await ctx.organization(session, grant.organization_id, Permission.ADMIN)
        check_version(grant.version, expected)
        if grant.revoked_at is None:
            grant.revoked_at = utcnow()
            touch(grant)
            await _audit_org(session, ctx, grant.organization_id, "support_grant.revoke", grant.id)
    return Response(status_code=204)
