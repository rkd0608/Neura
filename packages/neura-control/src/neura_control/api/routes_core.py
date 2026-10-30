"""Organizations, projects, sites, enrollment tokens, servers and robots."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import ReasonCode
from neura_control import installer
from neura_control.api import serializers
from neura_control.api.auth import authenticate
from neura_control.api.common import (
    ApiResult,
    apply_merge_patch,
    check_version,
    idempotent,
    if_match,
    page_query,
    paginate,
    parse_body,
    resource_result,
    transaction,
    validate,
)
from neura_control.api.schemas import (
    MUTABLE_FIELDS,
    EnrollmentTokenCreate,
    ProjectCreate,
    ProjectSpec,
    RobotCreate,
    RobotSpec,
    ServerSpec,
    SiteCreate,
    SiteSpec,
    SpecPatch,
)
from neura_control.api.scope import Ctx
from neura_control.clock import iso, utcnow
from neura_control.db.models import (
    ContractDocument,
    Deployment,
    EnrollmentToken,
    Project,
    PublisherKey,
    Robot,
    Server,
    Site,
)
from neura_control.ids import Kind, new_id
from neura_control.problems import ProblemError
from neura_control.security.principal import Permission, Principal
from neura_control.security.tokens import (
    ENROLLMENT_TOKEN_PREFIX,
    format_token,
    hash_secret,
    new_secret,
)
from neura_control.services.records import touch
from neura_control.settings import ControlPlaneSettings

router = APIRouter()
S = TypeVar("S", bound=BaseModel)


async def flush_unique(session: AsyncSession, what: str) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProblemError(
            ReasonCode.API_CONFLICT, f"a {what} with this name already exists"
        ) from exc


async def patched_spec[S: BaseModel](
    request: Request, current: dict[str, Any], kind: str, model: type[S]
) -> S:
    body = await parse_body(request, SpecPatch, merge_patch=True)
    immutable = sorted(set(body.spec) - MUTABLE_FIELDS[kind])
    if immutable:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED,
            f"these fields cannot be changed: {', '.join(immutable)}",
        )
    return validate(model, apply_merge_patch(current, body.spec))


# --- organizations and projects ----------------------------------------------------------------


@router.get("/v1/organizations/{organization_id}")
async def get_organization(
    organization_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        org = await Ctx(request, principal).organization(session, organization_id, Permission.READ)
        return resource_result(200, serializers.organization(org), org.version).response()


@router.get("/v1/organizations/{organization_id}/projects")
async def list_projects(
    organization_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).organization(session, organization_id, Permission.READ)
        statement = select(Project).where(Project.organization_id == organization_id)
        if principal.project_ids is not None:
            statement = statement.where(Project.id.in_(principal.project_ids))
        page = await paginate(
            session, statement, Project.id, query, serializers.project, Project.labels
        )
    return ApiResult(200, page).response()


@router.post("/v1/organizations/{organization_id}/projects", status_code=201)
async def create_project(
    organization_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, ProjectCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        await ctx.organization(session, organization_id, Permission.ADMIN)
        project = Project(
            id=new_id(Kind.PROJECT),
            organization_id=organization_id,
            name=body.spec.name,
            display_name=body.spec.display_name,
            labels=body.spec.labels,
            created_by=principal.subject,
        )
        session.add(project)
        await flush_unique(session, "project")
        await ctx.record(
            session, project, "project.create", project.id, version=1, event_type="resource.created"
        )
        await session.refresh(project)
        return resource_result(201, serializers.project(project), 1, f"/v1/projects/{project.id}")

    return await idempotent(request, principal, work)


@router.get("/v1/projects/{project_id}")
async def get_project(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        project = await Ctx(request, principal).project(session, project_id, Permission.READ)
        return resource_result(200, serializers.project(project), project.version).response()


@router.patch("/v1/projects/{project_id}")
async def update_project(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        project = await ctx.project(session, project_id, Permission.ADMIN)
        await session.refresh(project, with_for_update=True)
        check_version(project.version, expected)
        current = {
            "name": project.name,
            "display_name": project.display_name,
            "labels": project.labels,
        }
        spec = await patched_spec(request, current, "project", ProjectSpec)
        project.display_name, project.labels = spec.display_name, spec.labels
        touch(project)
        await ctx.record(
            session,
            project,
            "project.update",
            project.id,
            version=project.version,
            event_type="resource.updated",
        )
        return resource_result(200, serializers.project(project), project.version).response()


# --- sites -------------------------------------------------------------------------------------


@router.get("/v1/projects/{project_id}/sites")
async def list_sites(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        page = await paginate(
            session,
            select(Site).where(Site.project_id == project_id),
            Site.id,
            query,
            serializers.site,
            Site.labels,
        )
    return ApiResult(200, page).response()


@router.post("/v1/projects/{project_id}/sites", status_code=201)
async def create_site(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, SiteCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        project = await ctx.project(session, project_id, Permission.ADMIN)
        site = Site(
            id=new_id(Kind.SITE),
            project_id=project.id,
            name=body.spec.name,
            display_name=body.spec.display_name,
            labels=body.spec.labels,
            offline_authorization_hours=body.spec.offline_authorization_hours,
            kind=body.spec.kind,
            observed={"phase": "Unknown", "connected": False},
            created_by=principal.subject,
        )
        session.add(site)
        await flush_unique(session, "site")
        await ctx.record(
            session, project, "site.create", site.id, version=1, event_type="resource.created"
        )
        await session.refresh(site)
        return resource_result(201, serializers.site(site), 1, f"/v1/sites/{site.id}")

    return await idempotent(request, principal, work)


@router.get("/v1/sites/{site_id}")
async def get_site(
    site_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        site, _ = await Ctx(request, principal).owned(session, Site, site_id, Permission.READ)
        return resource_result(200, serializers.site(site), site.version).response()


@router.patch("/v1/sites/{site_id}")
async def update_site(
    site_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        site, project = await ctx.owned(session, Site, site_id, Permission.ADMIN, lock=True)
        check_version(site.version, expected)
        current = {
            "name": site.name,
            "display_name": site.display_name,
            "labels": site.labels,
            "offline_authorization_hours": site.offline_authorization_hours,
            "kind": site.kind,
        }
        spec = await patched_spec(request, current, "site", SiteSpec)
        site.display_name, site.labels = spec.display_name, spec.labels
        site.offline_authorization_hours = spec.offline_authorization_hours
        touch(site)
        await ctx.record(
            session,
            project,
            "site.update",
            site.id,
            version=site.version,
            event_type="resource.updated",
        )
        return resource_result(200, serializers.site(site), site.version).response()


@router.delete("/v1/sites/{site_id}", status_code=204)
async def delete_site(
    site_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        site, project = await ctx.owned(session, Site, site_id, Permission.ADMIN, lock=True)
        check_version(site.version, expected)
        for model, what in ((Robot, "robots"), (Server, "servers"), (Deployment, "deployments")):
            if await session.scalar(select(exists().where(model.site_id == site.id))):
                raise ProblemError(ReasonCode.API_CONFLICT, f"the site still has {what}")
        await session.delete(site)
        await ctx.record(
            session,
            project,
            "site.delete",
            site_id,
            version=site.version + 1,
            event_type="resource.deleted",
        )
    return Response(status_code=204)


@router.post("/v1/sites/{site_id}/enrollment-tokens", status_code=201)
async def create_enrollment_token(
    site_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, EnrollmentTokenCreate)
    ctx = Ctx(request, principal)
    settings: ControlPlaneSettings = request.app.state.settings

    async def work(session: AsyncSession) -> ApiResult:
        site, project = await ctx.owned(session, Site, site_id, Permission.ADMIN)
        ttl = body.ttl_minutes or settings.enrollment_token_default_ttl_minutes
        token_id = new_id(Kind.ENROLLMENT_TOKEN)
        secret = new_secret()
        expires_at = utcnow() + timedelta(minutes=ttl)
        session.add(
            EnrollmentToken(
                id=token_id,
                site_id=site.id,
                secret_hash=hash_secret(secret),
                expires_at=expires_at,
                created_by=principal.subject,
            )
        )
        await ctx.record(session, project, "enrollment_token.create", token_id)
        token = format_token(ENROLLMENT_TOKEN_PREFIX, token_id, secret)
        endpoint = settings.agent_enroll_endpoint or settings.agent_endpoint
        keys = (
            await session.scalars(
                select(PublisherKey.public_key)
                .where(PublisherKey.organization_id == project.organization_id)
                .order_by(PublisherKey.id)
            )
        ).all()
        flags = "".join(f" --trust {key}" for key in keys)
        if site.kind == "bench":
            flags += " --bench"
        enroll = f"neura-agent enroll --control-plane {endpoint} --token {token}"
        return ApiResult(
            201,
            {
                "id": token_id,
                "site_id": site.id,
                "secret": token,
                "expires_at": iso(expires_at),
                # A production server, run by its administrator: the one-line installer when this
                # control plane offers an agent release, else enrolling an agent installed with
                # deploy/install/install.sh.
                "install_command": installer.command(
                    settings, token, bench=site.kind == "bench", trust=list(keys)
                )
                if installer.available(settings)
                else enroll,
                # A workstation or a test bench: the agent for the current user, in the
                # current Python environment (neura site join).
                "join_command": (
                    f"neura site join {site.name} --control-plane {endpoint} --token {token}{flags}"
                ),
                # The agent alone, for scripts that install it themselves.
                "enroll_command": enroll,
            },
            {"Location": f"/v1/sites/{site.id}"},
        )

    return await idempotent(request, principal, work)


# --- servers -----------------------------------------------------------------------------------


@router.get("/v1/sites/{site_id}/servers")
async def list_servers(
    site_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).owned(session, Site, site_id, Permission.READ)
        page = await paginate(
            session,
            select(Server).where(Server.site_id == site_id),
            Server.id,
            query,
            serializers.server,
            Server.labels,
        )
    return ApiResult(200, page).response()


@router.get("/v1/servers/{server_id}")
async def get_server(
    server_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        server, _ = await Ctx(request, principal).owned(session, Server, server_id, Permission.READ)
        return resource_result(200, serializers.server(server), server.version).response()


@router.patch("/v1/servers/{server_id}")
async def update_server(
    server_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        server, project = await ctx.owned(session, Server, server_id, Permission.ADMIN, lock=True)
        check_version(server.version, expected)
        current = {
            "display_name": server.display_name,
            "labels": server.labels,
            "allocated_gpu_indexes": server.allocated_gpu_indexes,
        }
        spec = await patched_spec(request, current, "server", ServerSpec)
        known = {gpu.get("index") for gpu in server.observed.get("gpus", [])}
        unknown = sorted(set(spec.allocated_gpu_indexes) - known) if known else []
        if unknown:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED, f"the server has no GPUs {unknown}"
            )
        server.display_name, server.labels = spec.display_name, spec.labels
        server.allocated_gpu_indexes = list(spec.allocated_gpu_indexes)
        touch(server)
        await ctx.record(
            session,
            project,
            "server.update",
            server.id,
            version=server.version,
            event_type="resource.updated",
        )
        return resource_result(200, serializers.server(server), server.version).response()


@router.delete("/v1/servers/{server_id}", status_code=204)
async def delete_server(
    server_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        server, project = await ctx.owned(session, Server, server_id, Permission.ADMIN, lock=True)
        check_version(server.version, expected)
        await session.delete(server)
        await ctx.record(
            session,
            project,
            "server.delete",
            server_id,
            version=server.version + 1,
            event_type="resource.deleted",
        )
    return Response(status_code=204)


# --- robots ------------------------------------------------------------------------------------


async def _validate_robot_refs(session: AsyncSession, project_id: str, spec: RobotSpec) -> None:
    site = await session.get(Site, spec.site_id)
    if site is None or site.project_id != project_id:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, "site_id is not a site in this project"
        )
    profile = await session.get(ContractDocument, spec.robot_profile_id)
    if profile is None or profile.project_id != project_id or profile.kind != "RobotProfile":
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED,
            "robot_profile_id is not a registered robot profile in this project",
        )


@router.get("/v1/projects/{project_id}/robots")
async def list_robots(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        page = await paginate(
            session,
            select(Robot).where(Robot.project_id == project_id),
            Robot.id,
            query,
            serializers.robot,
            Robot.labels,
        )
    return ApiResult(200, page).response()


@router.post("/v1/projects/{project_id}/robots", status_code=201)
async def create_robot(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, RobotCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        project = await ctx.project(session, project_id, Permission.ADMIN)
        await _validate_robot_refs(session, project.id, body.spec)
        robot = Robot(
            id=new_id(Kind.ROBOT),
            project_id=project.id,
            site_id=body.spec.site_id,
            robot_profile_id=body.spec.robot_profile_id,
            name=body.spec.name,
            display_name=body.spec.display_name,
            labels=body.spec.labels,
            observed={"phase": "Unknown"},
            created_by=principal.subject,
        )
        session.add(robot)
        await flush_unique(session, "robot")
        await ctx.record(
            session, project, "robot.create", robot.id, version=1, event_type="resource.created"
        )
        await session.refresh(robot)
        return resource_result(201, serializers.robot(robot), 1, f"/v1/robots/{robot.id}")

    return await idempotent(request, principal, work)


@router.get("/v1/robots/{robot_id}")
async def get_robot(
    robot_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        robot, _ = await Ctx(request, principal).owned(session, Robot, robot_id, Permission.READ)
        return resource_result(200, serializers.robot(robot), robot.version).response()


@router.patch("/v1/robots/{robot_id}")
async def update_robot(
    robot_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        robot, project = await ctx.owned(session, Robot, robot_id, Permission.ADMIN, lock=True)
        check_version(robot.version, expected)
        current = {
            "name": robot.name,
            "display_name": robot.display_name,
            "site_id": robot.site_id,
            "robot_profile_id": robot.robot_profile_id,
            "labels": robot.labels,
        }
        spec = await patched_spec(request, current, "robot", RobotSpec)
        await _validate_robot_refs(session, project.id, spec)
        robot.display_name, robot.labels, robot.robot_profile_id = (
            spec.display_name,
            spec.labels,
            spec.robot_profile_id,
        )
        touch(robot)
        await ctx.record(
            session,
            project,
            "robot.update",
            robot.id,
            version=robot.version,
            event_type="resource.updated",
        )
        return resource_result(200, serializers.robot(robot), robot.version).response()


@router.delete("/v1/robots/{robot_id}", status_code=204)
async def delete_robot(
    robot_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        robot, project = await ctx.owned(session, Robot, robot_id, Permission.ADMIN, lock=True)
        check_version(robot.version, expected)
        in_use = await session.scalar(
            select(func.count())
            .select_from(Deployment)
            .where(Deployment.project_id == project.id, Deployment.robot_ids.contains([robot.id]))
        )
        if in_use:
            raise ProblemError(ReasonCode.API_CONFLICT, "the robot is part of a deployment")
        await session.delete(robot)
        await ctx.record(
            session,
            project,
            "robot.delete",
            robot_id,
            version=robot.version + 1,
            event_type="resource.deleted",
        )
    return Response(status_code=204)
