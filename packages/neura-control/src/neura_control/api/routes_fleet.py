"""The workspace, policies and their versions, rollouts, activity and publisher keys (design §29).

These are the resources the CLI and the console are built on: a person names a policy version and
some robots; a rollout does the rest on the revisions, operations and activation requests below
it, and says in plain words what it is doing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import ContractError, ReasonCode, parse_document, signing
from neura_control.api import serializers
from neura_control.api.auth import authenticate
from neura_control.api.common import (
    ApiResult,
    check_version,
    encode_cursor,
    idempotent,
    if_match,
    page_query,
    paginate,
    parse_body,
    resource_result,
    transaction,
)
from neura_control.api.routes_core import flush_unique, patched_spec
from neura_control.api.schemas import (
    PolicyCreate,
    PolicySpec,
    PolicyVersionCreate,
    PublisherKeyCreate,
    RolloutCreate,
)
from neura_control.api.scope import Ctx
from neura_control.db.models import (
    ContractDocument,
    Event,
    Policy,
    PolicyVersion,
    Project,
    PublisherKey,
    Robot,
    Rollout,
    Site,
)
from neura_control.ids import Kind, new_id
from neura_control.problems import ProblemError
from neura_control.security.principal import Permission, Principal
from neura_control.services import contracts, records, rollouts, workspace
from neura_control.services.records import touch

router = APIRouter()

# Which adapters each source kind may name: a version's source says how it was built, and the
# artifact's adapter is what the site loads it with.
SOURCE_ADAPTERS = {
    "lerobot": lambda adapter: adapter.startswith("lerobot-"),
    "python": lambda adapter: adapter == "python",
    "container": lambda adapter: adapter == "container",
}


async def _desc_page(
    session: AsyncSession,
    statement: Any,
    id_column: Any,
    request: Request,
    serialize: Any,
) -> dict[str, Any]:
    """Newest first: the cursor is the last id of the previous page."""
    query = page_query(request)
    if query.cursor is not None:
        statement = statement.where(id_column < query.cursor)
    rows = list(
        await session.scalars(statement.order_by(id_column.desc()).limit(query.page_size + 1))
    )
    more = len(rows) > query.page_size
    rows = rows[: query.page_size]
    items = [await serialize(session, r) for r in rows]
    cursor = encode_cursor(str(getattr(rows[-1], id_column.key))) if more and rows else None
    return {"items": items, "page": {"next_cursor": cursor}}


# --- workspace ---------------------------------------------------------------------------------


@router.get("/v1/workspace")
async def get_workspace(request: Request, principal: Principal = Depends(authenticate)) -> Response:
    async with transaction(request) as session:
        org = await workspace.organization(
            session, principal, request.query_params.get("organization_id")
        )
        project = await workspace.default_project(session, principal, org)
        return ApiResult(
            200,
            {
                "organization_id": org.id,
                "display_name": org.display_name,
                "project_id": project.id,
                "permissions": sorted(principal.permissions(org.id, project.id)),
            },
        ).response()


# --- policies ----------------------------------------------------------------------------------


async def _policy_json(session: AsyncSession, row: Policy) -> dict[str, Any]:
    count, latest = (
        await session.execute(
            select(func.count(PolicyVersion.id), func.max(PolicyVersion.number)).where(
                PolicyVersion.policy_id == row.id
            )
        )
    ).one()
    return serializers.policy(row, versions=int(count), latest=latest)


async def _version_json(session: AsyncSession, row: PolicyVersion) -> dict[str, Any]:
    policy = await session.get(Policy, row.policy_id)
    artifact = await session.get(ContractDocument, row.model_artifact_id)
    assert policy is not None
    assert artifact is not None
    running = list(
        (
            await session.scalars(
                select(Robot.id)
                .where(
                    Robot.project_id == row.project_id,
                    Robot.observed["policy_version_id"].astext == row.id,
                )
                .order_by(Robot.id)
            )
        ).all()
    )
    return serializers.policy_version(
        row, policy_name=policy.name, artifact=artifact, running_robot_ids=running
    )


@router.get("/v1/projects/{project_id}/policies")
async def list_policies(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        rows: list[Policy] = []

        def keep(row: Policy) -> dict[str, Any]:
            rows.append(row)
            return {}

        page = await paginate(
            session,
            select(Policy).where(Policy.project_id == project_id),
            Policy.id,
            query,
            keep,
            Policy.labels,
        )
        page["items"] = [await _policy_json(session, r) for r in rows]
    return ApiResult(200, page).response()


@router.post("/v1/projects/{project_id}/policies", status_code=201)
async def create_policy(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, PolicyCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        project = await ctx.project(session, project_id, Permission.DEPLOY)
        row = Policy(
            id=new_id(Kind.POLICY),
            project_id=project.id,
            name=body.spec.name,
            display_name=body.spec.display_name or body.spec.name,
            description=body.spec.description,
            labels=body.spec.labels,
            created_by=principal.subject,
        )
        session.add(row)
        await flush_unique(session, "policy")
        await ctx.record(
            session, project, "policy.create", row.id, version=1, event_type="resource.created"
        )
        await session.refresh(row)
        return resource_result(201, await _policy_json(session, row), 1, f"/v1/policies/{row.id}")

    return await idempotent(request, principal, work)


@router.get("/v1/policies/{policy_id}")
async def get_policy(
    policy_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        row, _ = await Ctx(request, principal).owned(session, Policy, policy_id, Permission.READ)
        return resource_result(200, await _policy_json(session, row), row.version).response()


@router.patch("/v1/policies/{policy_id}")
async def update_policy(
    policy_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        row, project = await ctx.owned(session, Policy, policy_id, Permission.DEPLOY, lock=True)
        check_version(row.version, expected)
        current = {
            "name": row.name,
            "display_name": row.display_name,
            "description": row.description,
            "labels": row.labels,
        }
        spec = await patched_spec(request, current, "policy", PolicySpec)
        row.display_name = spec.display_name or row.name
        row.description, row.labels = spec.description, spec.labels
        touch(row)
        await ctx.record(
            session,
            project,
            "policy.update",
            row.id,
            version=row.version,
            event_type="resource.updated",
        )
        return resource_result(200, await _policy_json(session, row), row.version).response()


@router.get("/v1/policies/{policy_id}/versions")
async def list_policy_versions(
    policy_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        await Ctx(request, principal).owned(session, Policy, policy_id, Permission.READ)
        page = await _desc_page(
            session,
            select(PolicyVersion).where(PolicyVersion.policy_id == policy_id),
            PolicyVersion.id,
            request,
            _version_json,
        )
    return ApiResult(200, page).response()


@router.post("/v1/policies/{policy_id}/versions", status_code=201)
async def create_policy_version(
    policy_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    """A new version: its model artifact (with publisher signatures) and where it came from.
    Pushing an artifact a version already has returns that version."""
    body = await parse_body(request, PolicyVersionCreate)
    try:
        document = parse_document(body.spec.model_artifact.document)
    except ContractError as exc:
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, str(exc), errors=exc.errors) from exc
    if document.kind != "ModelArtifact":
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, f"a version is a ModelArtifact, not {document.kind}"
        )
    adapter = document.spec.adapter.name
    if not SOURCE_ADAPTERS[body.spec.source.kind](adapter):
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED,
            f"a {body.spec.source.kind} version cannot use the {adapter} adapter",
        )
    try:
        signatures = signing.validate(document.digest(), body.spec.model_artifact.signatures or [])
    except signing.SignatureError as exc:
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, str(exc)) from exc
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        policy, project = await ctx.owned(session, Policy, policy_id, Permission.DEPLOY, lock=True)
        artifact, _ = await contracts.record(
            session,
            project,
            document,
            principal=principal,
            request_id=ctx.request_id,
            signatures=signatures,
        )
        existing = (
            await session.scalars(
                select(PolicyVersion).where(
                    PolicyVersion.policy_id == policy.id,
                    PolicyVersion.model_artifact_id == artifact.id,
                )
            )
        ).first()
        if existing is not None:
            return resource_result(200, await _version_json(session, existing), existing.version)
        latest = await session.scalar(
            select(func.max(PolicyVersion.number)).where(PolicyVersion.policy_id == policy.id)
        )
        row = PolicyVersion(
            id=new_id(Kind.POLICY_VERSION),
            project_id=project.id,
            policy_id=policy.id,
            number=(latest or 0) + 1,
            model_artifact_id=artifact.id,
            source=body.spec.source.model_dump(),
            notes=body.spec.notes,
            created_by=principal.subject,
        )
        session.add(row)
        await session.flush()
        await records.audit(
            session,
            principal=principal,
            organization_id=project.organization_id,
            project_id=project.id,
            action="policy_version.create",
            target=row.id,
            request_id=ctx.request_id,
        )
        await records.event(
            session,
            project_id=project.id,
            type="resource.created",
            resource=row.id,
            version=1,
            summary=f"{principal.display} added {policy.name} v{row.number} "
            f"({body.spec.source.kind})",
        )
        await session.refresh(row)
        return resource_result(
            201, await _version_json(session, row), 1, f"/v1/policy-versions/{row.id}"
        )

    return await idempotent(request, principal, work)


@router.get("/v1/policy-versions/{policy_version_id}")
async def get_policy_version(
    policy_version_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        row, _ = await Ctx(request, principal).owned(
            session, PolicyVersion, policy_version_id, Permission.READ
        )
        return resource_result(200, await _version_json(session, row), row.version).response()


# --- rollouts ----------------------------------------------------------------------------------


async def _rollout_json(session: AsyncSession, row: Rollout) -> dict[str, Any]:
    return serializers.rollout(
        row, name=await rollouts.describe_version(session, row.policy_version_id)
    )


async def _robots_of(
    session: AsyncSession, project: Project, robot_ids: Sequence[str]
) -> tuple[Site, list[Robot]]:
    rows = (await session.scalars(select(Robot).where(Robot.id.in_(list(robot_ids))))).all()
    found = {r.id: r for r in rows if r.project_id == project.id}
    missing = [r for r in robot_ids if r not in found]
    if missing:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, f"{', '.join(missing)} are not robots in this project"
        )
    robots = [found[r] for r in robot_ids]
    sites = {r.site_id for r in robots}
    if len(sites) > 1:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED,
            "these robots are at different sites: roll out to each site on its own",
        )
    site = await session.get(Site, next(iter(sites)))
    assert site is not None
    return site, robots


@router.get("/v1/projects/{project_id}/rollouts")
async def list_rollouts(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    params = request.query_params
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        statement = select(Rollout).where(Rollout.project_id == project_id)
        if params.get("site_id"):
            statement = statement.where(Rollout.site_id == params["site_id"])
        if params.get("deployment_id"):
            statement = statement.where(Rollout.deployment_id == params["deployment_id"])
        if params.get("robot_id"):
            statement = statement.where(Rollout.robot_ids.contains([params["robot_id"]]))
        if params.get("state") == "active":
            statement = statement.where(Rollout.state.in_(rollouts.ACTIVE))
        elif params.get("state"):
            statement = statement.where(Rollout.state == params["state"])
        page = await _desc_page(session, statement, Rollout.id, request, _rollout_json)
    return ApiResult(200, page).response()


@router.post("/v1/projects/{project_id}/rollouts", status_code=201)
async def create_rollout(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    """Deploy a policy version to robots, try it on a bench robot, or roll robots back. The
    rollout's steps then advance with the site's reports; approval only ever happens at the
    site."""
    body = await parse_body(request, RolloutCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        project = await ctx.project(session, project_id, Permission.DEPLOY)
        spec = body.spec
        site, robots = await _robots_of(session, project, spec.robot_ids)
        if spec.kind == "rollback":
            row = await rollouts.plan_rollback(
                session, project, principal, site=site, robots=robots, note=spec.note
            )
        else:
            assert spec.policy_version_id is not None
            version = await session.get(PolicyVersion, spec.policy_version_id)
            if version is None or version.project_id != project.id:
                raise ProblemError(
                    ReasonCode.API_VALIDATION_FAILED,
                    f"{spec.policy_version_id} is not a policy version in this project",
                )
            row = await rollouts.plan_deploy(
                session,
                project,
                principal,
                site=site,
                robots=robots,
                version=version,
                shadow_s=spec.shadow_s,
                note=spec.note,
                kind=spec.kind,
            )
        await records.audit(
            session,
            principal=principal,
            organization_id=project.organization_id,
            project_id=project.id,
            action=f"rollout.{spec.kind}",
            target=row.id,
            request_id=ctx.request_id,
        )
        await session.flush()
        await session.refresh(row)
        return resource_result(
            201, await _rollout_json(session, row), row.version, f"/v1/rollouts/{row.id}"
        )

    return await idempotent(request, principal, work)


@router.get("/v1/rollouts/{rollout_id}")
async def get_rollout(
    rollout_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        row, _ = await Ctx(request, principal).owned(session, Rollout, rollout_id, Permission.READ)
        return resource_result(200, await _rollout_json(session, row), row.version).response()


@router.post("/v1/rollouts/{rollout_id}/cancel")
async def cancel_rollout(
    rollout_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        row, project = await ctx.owned(session, Rollout, rollout_id, Permission.DEPLOY, lock=True)
        await rollouts.cancel(session, row, principal)
        await records.audit(
            session,
            principal=principal,
            organization_id=project.organization_id,
            project_id=project.id,
            action="rollout.cancel",
            target=row.id,
            request_id=ctx.request_id,
        )
        await session.flush()
        await session.refresh(row)
        return resource_result(200, await _rollout_json(session, row), row.version)

    return await idempotent(request, principal, work)


# --- activity ----------------------------------------------------------------------------------


@router.get("/v1/projects/{project_id}/activity")
async def list_activity(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    """What happened, newest first, one sentence each. The live stream of the same events is
    ``/v1/projects/{project_id}/events``."""
    params = request.query_params
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        statement = select(Event).where(Event.project_id == project_id, Event.summary.is_not(None))
        for name, column in (
            ("site_id", Event.site_id),
            ("robot_id", Event.robot_id),
            ("rollout_id", Event.rollout_id),
        ):
            if params.get(name):
                statement = statement.where(column == params[name])
        if query.cursor is not None:
            try:
                before = int(query.cursor)
            except ValueError as exc:
                raise ProblemError(ReasonCode.API_VALIDATION_FAILED, "invalid cursor") from exc
            statement = statement.where(Event.cursor < before)
        rows = list(
            (
                await session.scalars(
                    statement.order_by(Event.cursor.desc()).limit(query.page_size + 1)
                )
            ).all()
        )
        more = len(rows) > query.page_size
        rows = rows[: query.page_size]
        cursor = encode_cursor(str(rows[-1].cursor)) if more and rows else None
        page = {"items": [serializers.activity(r) for r in rows], "page": {"next_cursor": cursor}}
    return ApiResult(200, page).response()


# --- publisher keys ----------------------------------------------------------------------------


@router.get("/v1/organizations/{organization_id}/publisher-keys")
async def list_publisher_keys(
    organization_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).organization(session, organization_id, Permission.READ)
        page = await paginate(
            session,
            select(PublisherKey).where(PublisherKey.organization_id == organization_id),
            PublisherKey.id,
            query,
            serializers.publisher_key,
        )
    return ApiResult(200, page).response()


@router.post("/v1/organizations/{organization_id}/publisher-keys", status_code=201)
async def create_publisher_key(
    organization_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    """Register the public part of a key this workspace signs artifacts with. New sites' install
    commands trust it; sites installed before must trust it locally."""
    body = await parse_body(request, PublisherKeyCreate)
    ctx = Ctx(request, principal)
    key_id = signing.key_id(bytes.fromhex(body.public_key))

    async def work(session: AsyncSession) -> ApiResult:
        org = await ctx.organization(session, organization_id, Permission.DEPLOY)
        existing = (
            await session.scalars(
                select(PublisherKey).where(
                    PublisherKey.organization_id == org.id, PublisherKey.key_id == key_id
                )
            )
        ).first()
        if existing is not None:
            return resource_result(200, serializers.publisher_key(existing), existing.version)
        row = PublisherKey(
            id=new_id(Kind.PUBLISHER_KEY),
            organization_id=org.id,
            key_id=key_id,
            public_key=body.public_key,
            display_name=body.display_name,
            created_by=principal.subject,
        )
        session.add(row)
        await session.flush()
        await records.audit(
            session,
            principal=principal,
            organization_id=org.id,
            project_id=None,
            action="publisher_key.create",
            target=row.id,
            request_id=ctx.request_id,
        )
        await session.refresh(row)
        return resource_result(
            201, serializers.publisher_key(row), 1, f"/v1/publisher-keys/{row.id}"
        )

    return await idempotent(request, principal, work)


@router.delete("/v1/publisher-keys/{publisher_key_id}", status_code=204)
async def delete_publisher_key(
    publisher_key_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        row = await session.get(PublisherKey, publisher_key_id, with_for_update=True)
        if row is None:
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        await ctx.organization(session, row.organization_id, Permission.ADMIN)
        check_version(row.version, expected)
        await session.delete(row)
        await records.audit(
            session,
            principal=principal,
            organization_id=row.organization_id,
            project_id=None,
            action="publisher_key.delete",
            target=row.id,
            request_id=ctx.request_id,
        )
    return Response(status_code=204)
