"""Deployments, revisions, preparation, activation requests, rollbacks and operations.

Site-dependent work returns 202 with an Operation that only a site acknowledgement completes
(design §20.4). Activation is a request: motion needs local approval at the site (design §20.9).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import ReasonCode, check_compatibility, content_digest
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
from neura_control.api.routes_contracts import load_contract, report_json
from neura_control.api.routes_core import flush_unique, patched_spec
from neura_control.api.schemas import (
    ActivationRequestCreate,
    DeploymentCreate,
    DeploymentSpec,
    RevisionCreate,
    RollbackCreate,
)
from neura_control.api.scope import Ctx, touch
from neura_control.db.models import (
    ActivationRequest,
    Deployment,
    Operation,
    Project,
    Revision,
    Robot,
    Site,
    SiteCommand,
)
from neura_control.ids import Kind, new_id
from neura_control.problems import ProblemError
from neura_control.security.principal import Permission, Principal
from neura_control.services.site_reports import update_operation

router = APIRouter()


async def _validate_deployment(
    session: AsyncSession, project_id: str, spec: DeploymentSpec, deployment_id: str | None
) -> None:
    site = await session.get(Site, spec.site_id)
    if site is None or site.project_id != project_id:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, "site_id is not a site in this project"
        )
    robots = (await session.scalars(select(Robot).where(Robot.id.in_(spec.robot_ids)))).all()
    found = {r.id: r for r in robots}
    for robot_id in spec.robot_ids:
        robot = found.get(robot_id)
        if robot is None or robot.project_id != project_id:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED, f"{robot_id} is not a robot in this project"
            )
        if robot.site_id != spec.site_id:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED, f"{robot_id} is not at site {spec.site_id}"
            )
    others = (
        await session.scalars(
            select(Deployment).where(
                Deployment.project_id == project_id, Deployment.id != (deployment_id or "")
            )
        )
    ).all()
    for other in others:
        overlap = set(other.robot_ids) & set(spec.robot_ids)
        if overlap:
            raise ProblemError(
                ReasonCode.API_CONFLICT,
                f"robots {sorted(overlap)} are already in deployment {other.id}",
            )
    if spec.desired_revision_id is not None:
        revision = await session.get(Revision, spec.desired_revision_id)
        if revision is None or revision.deployment_id != deployment_id:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED,
                "desired_revision_id is not a revision of this deployment",
            )
        if revision.phase in ("Failed", "Retired"):
            raise ProblemError(
                ReasonCode.API_CONFLICT, f"revision {revision.id} is {revision.phase}"
            )


def _spec_dict(row: Deployment) -> dict[str, Any]:
    spec: dict[str, Any] = serializers.deployment(row)["spec"]
    return spec


@router.get("/v1/projects/{project_id}/deployments")
async def list_deployments(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        page = await paginate(
            session,
            select(Deployment).where(Deployment.project_id == project_id),
            Deployment.id,
            query,
            serializers.deployment,
            Deployment.labels,
        )
    return ApiResult(200, page).response()


@router.post("/v1/projects/{project_id}/deployments", status_code=201)
async def create_deployment(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, DeploymentCreate)
    if body.spec.desired_revision_id is not None:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, "a new deployment has no revisions yet"
        )
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        project = await ctx.project(session, project_id, Permission.DEPLOY)
        await _validate_deployment(session, project.id, body.spec, None)
        deployment = Deployment(
            id=new_id(Kind.DEPLOYMENT),
            project_id=project.id,
            site_id=body.spec.site_id,
            name=body.spec.name,
            display_name=body.spec.display_name,
            robot_ids=list(body.spec.robot_ids),
            desired_revision_id=None,
            rollout=body.spec.rollout.model_dump(exclude_none=True),
            labels=body.spec.labels,
            observed={
                "phase": "Pending",
                "active_revision_ids": dict.fromkeys(body.spec.robot_ids),
            },
            created_by=principal.subject,
        )
        session.add(deployment)
        await flush_unique(session, "deployment")
        await ctx.record(
            session,
            project,
            "deployment.create",
            deployment.id,
            version=1,
            event_type="resource.created",
        )
        await session.refresh(deployment)
        return resource_result(
            201, serializers.deployment(deployment), 1, f"/v1/deployments/{deployment.id}"
        )

    return await idempotent(request, principal, work)


@router.get("/v1/deployments/{deployment_id}")
async def get_deployment(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        row, _ = await Ctx(request, principal).owned(
            session, Deployment, deployment_id, Permission.READ
        )
        return resource_result(200, serializers.deployment(row), row.version).response()


@router.patch("/v1/deployments/{deployment_id}")
async def update_deployment(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        row, project = await ctx.owned(
            session, Deployment, deployment_id, Permission.DEPLOY, lock=True
        )
        check_version(row.version, expected)
        spec = await patched_spec(request, _spec_dict(row), "deployment", DeploymentSpec)
        await _validate_deployment(session, project.id, spec, row.id)
        row.display_name, row.labels = spec.display_name, spec.labels
        row.robot_ids = list(spec.robot_ids)
        row.desired_revision_id = spec.desired_revision_id
        touch(row)
        await ctx.record(
            session,
            project,
            "deployment.update",
            row.id,
            version=row.version,
            event_type="resource.updated",
        )
        return resource_result(200, serializers.deployment(row), row.version).response()


@router.delete("/v1/deployments/{deployment_id}", status_code=204)
async def delete_deployment(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        row, project = await ctx.owned(
            session, Deployment, deployment_id, Permission.DEPLOY, lock=True
        )
        check_version(row.version, expected)
        active = [r for r, rev in row.observed.get("active_revision_ids", {}).items() if rev]
        if active:
            raise ProblemError(
                ReasonCode.API_CONFLICT, f"robots {sorted(active)} are running a revision of it"
            )
        await session.delete(row)
        await ctx.record(
            session,
            project,
            "deployment.delete",
            deployment_id,
            version=row.version + 1,
            event_type="resource.deleted",
        )
    return Response(status_code=204)


# --- revisions ---------------------------------------------------------------------------------


@router.get("/v1/deployments/{deployment_id}/revisions")
async def list_revisions(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).owned(session, Deployment, deployment_id, Permission.READ)
        page = await paginate(
            session,
            select(Revision).where(Revision.deployment_id == deployment_id),
            Revision.id,
            query,
            serializers.revision,
        )
    return ApiResult(200, page).response()


@router.post("/v1/deployments/{deployment_id}/revisions", status_code=201)
async def create_revision(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, RevisionCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        deployment, project = await ctx.owned(session, Deployment, deployment_id, Permission.DEPLOY)
        spec = body.spec
        artifact = await load_contract(session, project.id, spec.model_artifact_id, "ModelArtifact")
        robot_profile = await load_contract(
            session, project.id, spec.robot_profile_id, "RobotProfile"
        )
        profile = await load_contract(
            session, project.id, spec.execution_profile_id, "ExecutionProfile"
        )
        robots = (
            await session.scalars(select(Robot).where(Robot.id.in_(deployment.robot_ids)))
        ).all()
        mismatched = sorted(r.id for r in robots if r.robot_profile_id != spec.robot_profile_id)
        if mismatched:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED,
                f"robots {mismatched} use a different robot profile",
            )
        report = check_compatibility(artifact, robot_profile, profile)
        if not report.compatible:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED,
                "the artifact, robot profile and execution profile are not compatible",
                errors=[f"{i.code.value} at {i.path}: {i.message}" for i in report.issues],
            )
        revision = Revision(
            id=new_id(Kind.REVISION),
            project_id=project.id,
            deployment_id=deployment.id,
            model_artifact_id=spec.model_artifact_id,
            execution_profile_id=spec.execution_profile_id,
            robot_profile_id=spec.robot_profile_id,
            notes=spec.notes,
            revision_digest=content_digest(
                {
                    "model_artifact": report.artifact_digest,
                    "robot_profile": report.robot_profile_digest,
                    "execution_profile": report.execution_profile_digest,
                }
            ),
            compatibility=report_json(report),
            phase="Created",
            created_by=principal.subject,
        )
        session.add(revision)
        await session.flush()
        await ctx.record(
            session,
            project,
            "revision.create",
            revision.id,
            version=1,
            event_type="resource.created",
        )
        await session.refresh(revision)
        return resource_result(
            201, serializers.revision(revision), 1, f"/v1/revisions/{revision.id}"
        )

    return await idempotent(request, principal, work)


@router.get("/v1/revisions/{revision_id}")
async def get_revision(
    revision_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        row, _ = await Ctx(request, principal).owned(
            session, Revision, revision_id, Permission.READ
        )
        return resource_result(200, serializers.revision(row), row.version).response()


async def _start_operation(
    session: AsyncSession,
    ctx: Ctx,
    project: Project,
    *,
    site_id: str,
    kind: str,
    target: str,
    command: dict[str, Any],
) -> Operation:
    operation = Operation(
        id=new_id(Kind.OPERATION),
        project_id=project.id,
        site_id=site_id,
        kind=kind,
        target=target,
        state="Pending",
        created_by=ctx.principal.subject,
    )
    session.add(operation)
    await session.flush()
    command_id = new_id(Kind.OPERATION).replace("op_", "cmd_", 1)
    session.add(
        SiteCommand(
            site_id=site_id,
            command_id=command_id,
            operation_id=operation.id,
            command={**command, "command_id": command_id, "operation_id": operation.id},
        )
    )
    await ctx.record(
        session,
        project,
        f"operation.{kind}",
        operation.id,
        version=1,
        event_type="operation.updated",
    )
    await session.refresh(operation)
    return operation


def _accepted(operation: Operation) -> ApiResult:
    return ApiResult(
        202, serializers.operation(operation), {"Location": f"/v1/operations/{operation.id}"}
    )


@router.post("/v1/revisions/{revision_id}/prepare", status_code=202)
async def prepare_revision(
    revision_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        revision, project = await ctx.owned(
            session, Revision, revision_id, Permission.DEPLOY, lock=True
        )
        if revision.phase in ("Failed", "Retired"):
            raise ProblemError(ReasonCode.API_CONFLICT, f"the revision is {revision.phase}")
        deployment = await session.get(Deployment, revision.deployment_id)
        assert deployment is not None
        operation = await _start_operation(
            session,
            ctx,
            project,
            site_id=deployment.site_id,
            kind="prepare_revision",
            target=revision.id,
            command={
                "kind": "prepare",
                "revision_id": revision.id,
                "revision_digest": revision.revision_digest,
            },
        )
        if revision.phase == "Created":
            revision.phase = "Preparing"
            touch(revision)
        return _accepted(operation)

    return await idempotent(request, principal, work)


async def _deployment_revision(
    session: AsyncSession, deployment: Deployment, revision_id: str
) -> Revision:
    revision = await session.get(Revision, revision_id)
    if revision is None or revision.deployment_id != deployment.id:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, f"{revision_id} is not a revision of this deployment"
        )
    if revision.phase != "Prepared":
        raise ProblemError(
            ReasonCode.API_CONFLICT,
            f"revision {revision_id} is {revision.phase}; prepare it at the site first",
        )
    return revision


def _target_robots(deployment: Deployment, requested: list[str] | None) -> list[str]:
    if requested is None:
        canary = deployment.rollout.get("canary_robot_id")
        return [canary] if canary else list(deployment.robot_ids)
    unknown = sorted(set(requested) - set(deployment.robot_ids))
    if unknown or not requested:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, f"robots {unknown} are not in this deployment"
        )
    return list(dict.fromkeys(requested))


@router.get("/v1/deployments/{deployment_id}/activation-requests")
async def list_activation_requests(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).owned(session, Deployment, deployment_id, Permission.READ)
        page = await paginate(
            session,
            select(ActivationRequest).where(ActivationRequest.deployment_id == deployment_id),
            ActivationRequest.id,
            query,
            serializers.activation_request,
        )
    return ApiResult(200, page).response()


@router.post("/v1/deployments/{deployment_id}/activation-requests", status_code=202)
async def request_activation(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, ActivationRequestCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        deployment, project = await ctx.owned(session, Deployment, deployment_id, Permission.DEPLOY)
        revision = await _deployment_revision(session, deployment, body.revision_id)
        robots = _target_robots(deployment, body.robot_ids)
        operation = await _start_operation(
            session,
            ctx,
            project,
            site_id=deployment.site_id,
            kind="activate_revision",
            target=revision.id,
            command={"kind": "request_activation", "revision_id": revision.id, "robot_ids": robots},
        )
        session.add(
            ActivationRequest(
                id=new_id(Kind.ACTIVATION_REQUEST),
                project_id=project.id,
                deployment_id=deployment.id,
                revision_id=revision.id,
                robot_ids=robots,
                operation_id=operation.id,
                state="AwaitingLocalApproval",
                note=body.note,
                created_by=principal.subject,
            )
        )
        return _accepted(operation)

    return await idempotent(request, principal, work)


@router.post("/v1/deployments/{deployment_id}/rollbacks", status_code=202)
async def rollback(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, RollbackCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        deployment, project = await ctx.owned(session, Deployment, deployment_id, Permission.DEPLOY)
        revision = await _deployment_revision(session, deployment, body.to_revision_id)
        robots = (
            _target_robots(deployment, body.robot_ids)
            if body.robot_ids
            else list(deployment.robot_ids)
        )
        operation = await _start_operation(
            session,
            ctx,
            project,
            site_id=deployment.site_id,
            kind="rollback",
            target=revision.id,
            command={"kind": "rollback", "to_revision_id": revision.id, "robot_ids": robots},
        )
        session.add(
            ActivationRequest(
                id=new_id(Kind.ACTIVATION_REQUEST),
                project_id=project.id,
                deployment_id=deployment.id,
                revision_id=revision.id,
                robot_ids=robots,
                operation_id=operation.id,
                state="AwaitingLocalApproval",
                note=body.note,
                created_by=principal.subject,
            )
        )
        return _accepted(operation)

    return await idempotent(request, principal, work)


# --- operations --------------------------------------------------------------------------------


@router.get("/v1/operations/{operation_id}")
async def get_operation(
    operation_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        row, _ = await Ctx(request, principal).owned(
            session, Operation, operation_id, Permission.READ
        )
        return resource_result(200, serializers.operation(row), row.version).response()


@router.post("/v1/operations/{operation_id}/cancel")
async def cancel_operation(
    operation_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        row, project = await ctx.owned(
            session, Operation, operation_id, Permission.DEPLOY, lock=True
        )
        if row.state not in ("Pending", "Running"):
            raise ProblemError(ReasonCode.API_CONFLICT, f"the operation already {row.state}")
        session.add(
            SiteCommand(
                site_id=row.site_id,
                command_id=new_id(Kind.OPERATION).replace("op_", "cmd_", 1),
                operation_id=row.id,
                command={"kind": "cancel", "operation_id": row.id},
            )
        )
        operation = await update_operation(
            session, row.id, "Cancelled", message=f"cancelled by {principal.subject}"
        )
        await ctx.record(session, project, "operation.cancel", row.id)
        return resource_result(200, serializers.operation(operation), operation.version)

    return await idempotent(request, principal, work)
