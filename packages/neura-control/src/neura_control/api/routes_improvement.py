"""Evaluations and training jobs: a site evaluates and improves its own models (design §28.3,
§28.4).

Both run at the deployment's site, on its data and its hardware. The control plane records what
was asked and what the site reported: verdicts, error statistics and counts, never an episode. A
training job that succeeds adds a candidate revision to the deployment (``site_reports``), to
prepare, evaluate and activate like any other.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import ReasonCode
from neura_control.api import serializers
from neura_control.api.auth import authenticate
from neura_control.api.common import (
    ApiResult,
    idempotent,
    page_query,
    paginate,
    parse_body,
    resource_result,
    transaction,
)
from neura_control.api.routes_deployments import accepted, start_operation
from neura_control.api.schemas import (
    EpisodeFilter,
    EvaluationCreate,
    ReplaySuite,
    TrainingJobCreate,
)
from neura_control.api.scope import Ctx
from neura_control.db.models import (
    ContractDocument,
    Deployment,
    Evaluation,
    Revision,
    Robot,
    TrainingJob,
)
from neura_control.ids import Kind, new_id
from neura_control.problems import ProblemError
from neura_control.security.principal import Permission, Principal
from neura_control.services.manifests import LIVE_PHASES

router = APIRouter()


def running_revisions(deployment: Deployment) -> list[str]:
    """The revisions the deployment's robots run, the one most of them run first."""
    counts = Counter(
        revision_id
        for revision_id in deployment.observed.get("active_revision_ids", {}).values()
        if revision_id
    )
    return [revision_id for revision_id, _ in counts.most_common()]


async def _revision(
    session: AsyncSession, deployment: Deployment, revision_id: str, role: str
) -> Revision:
    """A revision of the deployment that its site still has (in its manifest)."""
    revision = await session.get(Revision, revision_id)
    if revision is None or revision.deployment_id != deployment.id:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED,
            f"the {role} {revision_id} is not a revision of this deployment",
        )
    if revision.phase not in LIVE_PHASES and revision.id not in running_revisions(deployment):
        raise ProblemError(
            ReasonCode.API_CONFLICT,
            f"the {role} {revision_id} is {revision.phase}; its site no longer has it",
        )
    return revision


def _site_time(value: datetime) -> str:
    # The form a site records episodes' start times in, so its store compares them as text.
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


async def _site_filter(
    session: AsyncSession, deployment: Deployment, selection: EpisodeFilter
) -> dict[str, Any]:
    """The site store's filter for a selection: robots by the name the site knows them by, and
    only ever the deployment's own robots and revisions."""
    robots = (await session.scalars(select(Robot).where(Robot.id.in_(deployment.robot_ids)))).all()
    names = sorted(robot.name for robot in robots)
    revisions = list(
        (
            await session.scalars(
                select(Revision.id)
                .where(Revision.deployment_id == deployment.id)
                .order_by(Revision.id)
            )
        ).all()
    )
    unknown = sorted(set(selection.robot_names) - set(names))
    if unknown:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, f"robots {unknown} are not in this deployment"
        )
    unknown = sorted(set(selection.revision_ids) - set(revisions))
    if unknown:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED,
            f"revisions {unknown} are not revisions of this deployment",
        )
    site: dict[str, Any] = {
        "robot_ids": list(dict.fromkeys(selection.robot_names)) or names,
        "revision_ids": list(dict.fromkeys(selection.revision_ids)) or revisions,
        "outcomes": list(selection.outcomes),
        "end_reasons": list(selection.end_reasons),
        "complete_only": selection.complete_only,
    }
    if selection.since is not None:
        site["since"] = _site_time(selection.since)
    if selection.until is not None:
        site["until"] = _site_time(selection.until)
    if selection.limit is not None:
        site["limit"] = selection.limit
    return site


# --- evaluations -------------------------------------------------------------------------------


@router.get("/v1/deployments/{deployment_id}/evaluations")
async def list_evaluations(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).owned(session, Deployment, deployment_id, Permission.READ)
        page = await paginate(
            session,
            select(Evaluation).where(Evaluation.deployment_id == deployment_id),
            Evaluation.id,
            query,
            serializers.evaluation,
        )
    return ApiResult(200, page).response()


@router.post("/v1/deployments/{deployment_id}/evaluations", status_code=202)
async def create_evaluation(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, EvaluationCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        deployment, project = await ctx.owned(session, Deployment, deployment_id, Permission.DEPLOY)
        spec = body.spec
        candidate = await _revision(session, deployment, spec.candidate_revision_id, "candidate")
        baseline_id = spec.baseline_revision_id
        if baseline_id is None:
            # The revision it would replace: what most of the deployment's robots run.
            baseline_id = next(
                (r for r in running_revisions(deployment) if r != candidate.id), None
            )
        if baseline_id is not None:
            await _revision(session, deployment, baseline_id, "baseline")
        elif spec.suite.kind in ("simulation", "shadow"):
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED,
                "the deployment's robots run no other revision to compare with; give "
                "baseline_revision_id",
            )
        if spec.suite.kind == "shadow" and baseline_id not in running_revisions(deployment):
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED,
                "a shadow compares with a revision the deployment's robots run",
            )
        suite = spec.suite.model_dump(mode="json", exclude_none=True)
        command_suite = dict(suite)
        if isinstance(spec.suite, ReplaySuite):
            command_suite["filter"] = await _site_filter(session, deployment, spec.suite.filter)
        evaluation = Evaluation(
            id=new_id(Kind.EVALUATION),
            project_id=project.id,
            deployment_id=deployment.id,
            candidate_revision_id=candidate.id,
            baseline_revision_id=baseline_id,
            suite=suite,
            phase="Pending",
            created_by=principal.subject,
        )
        session.add(evaluation)
        await session.flush()
        operation = await start_operation(
            session,
            ctx,
            project,
            site_id=deployment.site_id,
            kind="evaluate",
            target=evaluation.id,
            command={
                "kind": "evaluate",
                "evaluation_id": evaluation.id,
                "candidate_revision_id": candidate.id,
                "baseline_revision_id": baseline_id,
                "suite": command_suite,
            },
        )
        evaluation.operation_id = operation.id
        await ctx.record(
            session,
            project,
            "evaluation.create",
            evaluation.id,
            version=1,
            event_type="resource.created",
        )
        return accepted(operation)

    return await idempotent(request, principal, work)


@router.get("/v1/evaluations/{evaluation_id}")
async def get_evaluation(
    evaluation_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        row, _ = await Ctx(request, principal).owned(
            session, Evaluation, evaluation_id, Permission.READ
        )
        return resource_result(200, serializers.evaluation(row), row.version).response()


# --- training jobs -----------------------------------------------------------------------------


@router.get("/v1/deployments/{deployment_id}/training-jobs")
async def list_training_jobs(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).owned(session, Deployment, deployment_id, Permission.READ)
        page = await paginate(
            session,
            select(TrainingJob).where(TrainingJob.deployment_id == deployment_id),
            TrainingJob.id,
            query,
            serializers.training_job,
        )
    return ApiResult(200, page).response()


async def _artifact_named(
    session: AsyncSession, project_id: str, name: str, version: str
) -> str | None:
    """A registered artifact, or a training job that will register one, with this name and
    version: one name and version names one model."""
    registered = (
        await session.scalars(
            select(ContractDocument.id).where(
                ContractDocument.project_id == project_id,
                ContractDocument.kind == "ModelArtifact",
                ContractDocument.document["metadata"]["name"].astext == name,
                ContractDocument.document["metadata"]["version"].astext == version,
            )
        )
    ).first()
    if registered is not None:
        return registered
    return (
        await session.scalars(
            select(TrainingJob.id).where(
                TrainingJob.project_id == project_id,
                TrainingJob.artifact_name == name,
                TrainingJob.artifact_version == version,
                TrainingJob.phase.in_(("Pending", "Running")),
            )
        )
    ).first()


@router.post("/v1/deployments/{deployment_id}/training-jobs", status_code=202)
async def create_training_job(
    deployment_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, TrainingJobCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        deployment, project = await ctx.owned(session, Deployment, deployment_id, Permission.DEPLOY)
        spec = body.spec
        base = await _revision(session, deployment, spec.base_revision_id, "base")
        artifact = await session.get(ContractDocument, base.model_artifact_id)
        assert artifact is not None
        job_id = new_id(Kind.TRAINING_JOB)
        metadata = artifact.document["metadata"]
        name = spec.artifact_name or metadata["name"]
        version = spec.artifact_version or f"{metadata['version'][:48]}-ft.{job_id[-8:].lower()}"
        taken = await _artifact_named(session, project.id, name, version)
        if taken is not None:
            raise ProblemError(
                ReasonCode.API_CONFLICT, f"{name} {version} is already taken by {taken}"
            )
        episodes = await _site_filter(session, deployment, spec.episodes)
        recipe = spec.recipe.model_dump(mode="json")
        job = TrainingJob(
            id=job_id,
            project_id=project.id,
            deployment_id=deployment.id,
            base_revision_id=base.id,
            episodes=spec.episodes.model_dump(mode="json", exclude_none=True),
            recipe=recipe,
            artifact_name=name,
            artifact_version=version,
            phase="Pending",
            created_by=principal.subject,
        )
        session.add(job)
        await session.flush()
        operation = await start_operation(
            session,
            ctx,
            project,
            site_id=deployment.site_id,
            kind="train",
            target=job.id,
            command={
                "kind": "train",
                "training_job_id": job.id,
                "base_revision_id": base.id,
                "episodes": episodes,
                "recipe": recipe,
                "artifact_name": name,
                "artifact_version": version,
            },
        )
        job.operation_id = operation.id
        await ctx.record(
            session,
            project,
            "training_job.create",
            job.id,
            version=1,
            event_type="resource.created",
        )
        return accepted(operation)

    return await idempotent(request, principal, work)


@router.get("/v1/training-jobs/{training_job_id}")
async def get_training_job(
    training_job_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        row, _ = await Ctx(request, principal).owned(
            session, TrainingJob, training_job_id, Permission.READ
        )
        return resource_result(200, serializers.training_job(row), row.version).response()
