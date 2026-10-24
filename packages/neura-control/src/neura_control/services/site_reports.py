"""Applying what sites report: the only path that writes observed state (design §20.2, §20.8).

The site agent's stream (M4) calls these functions with acknowledgements and status. API handlers
never do: a successful request is intent, and only a site report turns it into observed fact.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import (
    ContractError,
    ExecutionProfile,
    ModelArtifact,
    ReasonCode,
    RobotProfile,
    check_compatibility,
    parse_document,
    signing,
)
from neura_control.clock import iso, utcnow
from neura_control.db.models import (
    ActivationRequest,
    ContractDocument,
    Deployment,
    Evaluation,
    Fault,
    MetricPoint,
    Operation,
    Policy,
    PolicyVersion,
    Project,
    Revision,
    Robot,
    RobotSession,
    Server,
    Site,
    TrainingJob,
)
from neura_control.ids import Kind, new_id
from neura_control.security.principal import Principal, PrincipalKind
from neura_control.services import records
from neura_control.services.revisions import report_json, revision_digest

log = logging.getLogger("neura.control")
TERMINAL = frozenset({"Succeeded", "Failed", "Cancelled"})
VERDICTS = frozenset({"superior", "non_inferior", "inconclusive", "inferior"})


def _touch(row: Any) -> None:
    row.version += 1
    row.updated_at = utcnow()


async def _emit(
    session: AsyncSession, project_id: str, row: Any, type_: str = "resource.updated"
) -> None:
    await records.event(
        session, project_id=project_id, type=type_, resource=row.id, version=row.version
    )


async def update_operation(
    session: AsyncSession,
    operation_id: str,
    state: str,
    *,
    reason: ReasonCode | None = None,
    message: str = "",
    result: dict[str, Any] | None = None,
) -> Operation:
    """Advance an operation from a site acknowledgement and apply its consequences."""
    operation = await session.get(Operation, operation_id, with_for_update=True)
    if operation is None:
        raise LookupError(operation_id)
    if operation.state in TERMINAL:
        return operation  # late or duplicate acknowledgement
    now = utcnow()
    operation.state = state
    operation.reason = reason.value if reason else None
    operation.message = message
    if state == "Running" and operation.started_at is None:
        operation.started_at = now
    if state in TERMINAL:
        operation.finished_at = now
        operation.started_at = operation.started_at or now
    if result is not None:
        operation.result = result
    _touch(operation)
    await records.event(
        session,
        project_id=operation.project_id,
        type="operation.updated",
        resource=operation.id,
        version=operation.version,
    )
    if state in TERMINAL:
        await _apply_outcome(session, operation)
    elif operation.kind in ("evaluate", "train"):
        row = await _job(session, operation)
        if row is not None and (row.phase, row.message) != ("Running", operation.message):
            row.phase, row.message = "Running", operation.message
            _touch(row)
            await _emit(session, row.project_id, row)
    from neura_control.services import rollouts  # the driver starts operations itself

    await rollouts.on_operation(session, operation)
    return operation


class _CapacityAssumptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    control_period_ms: float = Field(ge=0)
    starvation_budget_ms: float = Field(ge=0)
    request_interval_ms: float = Field(ge=0)
    link_allowance_ms: float = Field(ge=0)
    dispatch_freshness_ms: float | None = Field(default=None, ge=0)
    acceptance_freshness_ms: float | None = Field(default=None, ge=0)
    max_batch: int = Field(ge=1)
    batch_window_ms: float = Field(default=0, ge=0, le=1000)
    reserve_fraction: float = Field(ge=0, le=1)
    latency_p99_ms_by_batch: dict[str, float]
    memory_available_mib: int | None = Field(default=None, ge=0)
    device: Annotated[str, StringConstraints(max_length=16)] | None = None
    co_located_groups: int = Field(default=0, ge=0, le=1000)


class _Capacity(BaseModel):
    """What a site may report as a capacity estimate (api/openapi.yaml CapacityEstimate)."""

    model_config = ConfigDict(extra="forbid")
    max_robots: int = Field(ge=0, le=100_000)
    limiting_factor: Literal[
        "deadline",
        "dispatch_freshness",
        "acceptance_freshness",
        "memory",
        "throughput",
        "search_limit",
    ]
    source: Literal["configured_profile", "measured_profile"]
    sharing_case: Literal[
        "same_weights", "multi_adapter", "co_located_fine_tunes", "different_architectures"
    ] = "same_weights"
    # Absent when the site runs minimal telemetry: the estimate's inputs stay at the site.
    assumptions: _CapacityAssumptions | None = None


def _capacity(reported: Any) -> dict[str, Any] | None:
    """Accept a site's capacity estimate only if it has the published shape."""
    if reported is None:
        return None
    try:
        parsed = _Capacity.model_validate(reported)
    except ValidationError:
        log.warning("ignored a malformed capacity estimate from a site")
        return None
    estimate = parsed.model_dump(mode="json")
    if estimate["assumptions"] is None:
        del estimate["assumptions"]
    return {**estimate, "estimated_at": iso(utcnow())}


async def _apply_outcome(session: AsyncSession, operation: Operation) -> None:
    succeeded = operation.state == "Succeeded"
    if operation.kind == "prepare_revision":
        revision = await session.get(Revision, operation.target, with_for_update=True)
        if revision is None:
            return
        revision.phase = (
            "Prepared" if succeeded else ("Failed" if operation.state == "Failed" else "Created")
        )
        revision.reason = operation.reason
        revision.message = operation.message
        if succeeded:
            revision.capacity = _capacity(operation.result.get("capacity"))
        _touch(revision)
        await _emit(session, revision.project_id, revision)
        if succeeded:
            deployment = await session.get(Deployment, revision.deployment_id, with_for_update=True)
            if deployment is not None:
                deployment.observed = {
                    **deployment.observed,
                    "prepared_revision_id": revision.id,
                    "phase": "Prepared",
                    "observed_at": iso(utcnow()),
                }
                _touch(deployment)
                await _emit(session, deployment.project_id, deployment)
    elif operation.kind in ("activate_revision", "rollback"):
        request = (
            await session.scalars(
                select(ActivationRequest)
                .where(ActivationRequest.operation_id == operation.id)
                .with_for_update()
            )
        ).first()
        if request is not None:
            if succeeded:
                request.state = "Activated"
                request.approved_by = operation.result.get("approved_by")
            elif operation.reason == ReasonCode.AGENT_BOUNDARY_DENIED.value or operation.result.get(
                "refused"
            ):
                request.state = "Refused"
            else:
                request.state = "Cancelled" if operation.state == "Cancelled" else "Failed"
            _touch(request)
            await _emit(session, request.project_id, request)
        if succeeded:
            revision_id = operation.result.get("revision_id")
            robot_ids = operation.result.get("robot_ids", [])
            if revision_id:
                await set_active_revision(session, revision_id, list(robot_ids))
    elif operation.kind in ("evaluate", "train"):
        row = await _job(session, operation)
        if row is None:
            return
        row.phase, row.reason, row.message = operation.state, operation.reason, operation.message
        row.finished_at = operation.finished_at
        if succeeded:
            try:
                if isinstance(row, Evaluation):
                    _evaluation_report(row, operation.result)
                else:
                    await _register_trained_model(session, operation, row)
            except _ResultRejectedError as exc:
                # The site said it succeeded, but what it sent does not hold up: the operation
                # failed, and nothing it sent is registered.
                operation.state, operation.reason, operation.message = (
                    "Failed",
                    exc.code.value,
                    str(exc),
                )
                row.phase, row.reason, row.message = "Failed", exc.code.value, str(exc)
        _touch(row)
        await _emit(session, row.project_id, row)


class _ResultRejectedError(Exception):
    def __init__(self, code: ReasonCode, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _job(session: AsyncSession, operation: Operation) -> Evaluation | TrainingJob | None:
    row: Evaluation | TrainingJob | None
    if operation.kind == "evaluate":
        row = await session.get(Evaluation, operation.target, with_for_update=True)
    else:
        row = await session.get(TrainingJob, operation.target, with_for_update=True)
    return row if row is not None and row.operation_id == operation.id else None


def _evaluation_report(row: Evaluation, result: dict[str, Any]) -> None:
    report = result.get("evaluation")
    kind = row.suite.get("kind")
    if not isinstance(report, dict) or report.get("kind") != kind:
        raise _ResultRejectedError(
            ReasonCode.EVALUATION_REPORT_INVALID, f"the site reported no {kind} evaluation"
        )
    verdict = report.get("verdict")
    row.report = report
    row.verdict = verdict if verdict in VERDICTS else None


async def _register_trained_model(
    session: AsyncSession, operation: Operation, job: TrainingJob
) -> None:
    """Check what the site trained against what was asked, then register its artifact and
    execution profile and add a candidate revision to the deployment."""
    result = operation.result
    base = await session.get(Revision, job.base_revision_id)
    if base is None:
        raise _ResultRejectedError(
            ReasonCode.TRAINING_RESULT_REJECTED, "the base revision no longer exists"
        )
    rows = {
        role: await session.get(ContractDocument, identifier)
        for role, identifier in (
            ("model_artifact", base.model_artifact_id),
            ("robot_profile", base.robot_profile_id),
            ("execution_profile", base.execution_profile_id),
        )
    }
    documents = {role: parse_document(row.document) for role, row in rows.items() if row}
    base_artifact, robot_profile, base_profile = (
        documents["model_artifact"],
        documents["robot_profile"],
        documents["execution_profile"],
    )

    def reject(message: str) -> _ResultRejectedError:
        return _ResultRejectedError(ReasonCode.TRAINING_RESULT_REJECTED, message)

    reported = result.get("artifact"), result.get("execution_profile")
    if not all(isinstance(document, dict) for document in reported):
        raise reject("the site did not report a model artifact and an execution profile")
    try:
        artifact, profile = (parse_document(document) for document in reported)
    except (ContractError, ValueError) as exc:
        raise reject(f"the site's documents are not valid: {exc}") from exc
    if not isinstance(artifact, ModelArtifact) or not isinstance(profile, ExecutionProfile):
        raise reject("the site did not report a model artifact and an execution profile")
    assert isinstance(base_artifact, ModelArtifact)
    assert isinstance(base_profile, ExecutionProfile)
    assert isinstance(robot_profile, RobotProfile)
    digest = artifact.digest()
    lineage = artifact.spec.lineage
    dataset_digest = result.get("dataset_digest")
    if digest != result.get("artifact_digest"):
        raise reject("the artifact's digest is not the one reported")
    if (artifact.metadata.name, artifact.metadata.version) != (
        job.artifact_name,
        job.artifact_version,
    ):
        raise reject(f"the artifact is not named {job.artifact_name} {job.artifact_version}")
    if lineage is None or lineage.base_artifact != base_artifact.digest():
        raise reject("the artifact does not name the base revision's artifact as its base")
    if lineage.training.job_id != job.id:
        raise reject("the artifact does not name this training job")
    if not any(d.digest == dataset_digest for d in lineage.datasets):
        raise reject("the artifact does not name the dataset it was reported trained on")
    unchanged = {"weights", "evidence", "lineage"}
    if artifact.spec.model_dump(exclude=unchanged) != base_artifact.spec.model_dump(
        exclude=unchanged
    ):
        raise reject("a fine-tune changes the weights only; the artifact changes more")
    served: dict[str, Any] = profile.model_dump(mode="json", exclude_none=True)["spec"]
    served["binding"]["model_artifact"] = base_artifact.digest()
    if (
        profile.spec.binding.model_artifact != digest
        or served != base_profile.model_dump(mode="json", exclude_none=True)["spec"]
    ):
        raise reject("the execution profile is not the base's, bound to the new artifact")
    try:
        signatures = signing.validate(digest, [result.get("artifact_signature") or {}])
    except signing.SignatureError as exc:
        raise reject(f"the artifact's signature is not valid: {exc}") from exc
    report = check_compatibility(artifact, robot_profile, profile)
    if not report.compatible:
        raise reject("the new artifact is not compatible with the deployment's robots")

    project = await session.get(Project, job.project_id)
    assert project is not None
    site = Principal(subject=f"site:{operation.site_id}", kind=PrincipalKind.SITE_AGENT)

    async def audit(action: str, target: str) -> None:
        await records.audit(
            session,
            principal=site,
            organization_id=project.organization_id,
            project_id=project.id,
            action=action,
            target=target,
            request_id=operation.id,
        )

    async def register(
        document: Any, kind: str, id_kind: Kind, action: str, signed: list[dict[str, str]]
    ) -> ContractDocument:
        existing = (
            await session.scalars(
                select(ContractDocument).where(
                    ContractDocument.project_id == job.project_id,
                    ContractDocument.kind == kind,
                    ContractDocument.digest == document.digest(),
                )
            )
        ).first()
        if existing is not None:
            merged = signing.validate(existing.digest, [*existing.signatures, *signed])
            if merged != existing.signatures:
                existing.signatures = merged
                _touch(existing)
                await audit(f"{action}.sign", existing.id)
                await _emit(session, job.project_id, existing)
            return existing
        row = ContractDocument(
            id=new_id(id_kind),
            project_id=job.project_id,
            kind=kind,
            digest=document.digest(),
            document=document.canonical_data(),
            signatures=signed,
            created_by=job.created_by,
        )
        session.add(row)
        await session.flush()
        await audit(f"{action}.register", row.id)
        await _emit(session, job.project_id, row, "resource.created")
        return row

    artifact_row = await register(
        artifact, "ModelArtifact", Kind.MODEL_ARTIFACT, "model_artifact", signatures
    )
    profile_row = await register(
        profile, "ExecutionProfile", Kind.EXECUTION_PROFILE, "execution_profile", []
    )
    episodes = result.get("dataset_episodes")
    revision = Revision(
        id=new_id(Kind.REVISION),
        project_id=job.project_id,
        deployment_id=job.deployment_id,
        model_artifact_id=artifact_row.id,
        execution_profile_id=profile_row.id,
        robot_profile_id=base.robot_profile_id,
        notes=(
            f"Fine-tuned by training job {job.id} from revision {base.id} on {episodes} "
            f"episodes recorded at the site (dataset {dataset_digest})."
        ),
        revision_digest=revision_digest(report),
        compatibility=report_json(report),
        phase="Created",
        created_by=job.created_by,
    )
    session.add(revision)
    await session.flush()
    await audit("revision.create", revision.id)
    await _emit(session, job.project_id, revision, "resource.created")
    training = result.get("training")
    job.artifact_id = artifact_row.id
    job.execution_profile_id = profile_row.id
    job.candidate_revision_id = revision.id
    job.dataset_digest = dataset_digest
    job.dataset_episodes = episodes if isinstance(episodes, int) else None
    job.metrics = _training_metrics(training) if isinstance(training, dict) else None


def _training_metrics(reported: dict[str, Any]) -> dict[str, Any]:
    """What a training report says about the run: the data and the loss."""
    dataset = reported.get("dataset") or {}
    training = reported.get("training") or {}
    metrics = {
        "episodes": dataset.get("episodes"),
        "frames": dataset.get("frames"),
        "episodes_skipped": dataset.get("skipped"),
        "steps": training.get("steps"),
        "seconds": training.get("seconds"),
        "loss_first": training.get("loss_first"),
        "loss_last": training.get("loss_last"),
        "losses": training.get("losses"),
    }
    return {
        k: v
        for k, v in metrics.items()
        if isinstance(v, int | float | list) and not isinstance(v, bool)
    }


async def _policy_version_of(session: AsyncSession, revision: Revision) -> dict[str, Any]:
    """The policy version a revision serves, as a robot's status shows it."""
    version = (
        await session.scalars(
            select(PolicyVersion).where(
                PolicyVersion.project_id == revision.project_id,
                PolicyVersion.model_artifact_id == revision.model_artifact_id,
            )
        )
    ).first()
    if version is None:
        return {"policy_version_id": None, "policy_name": None, "policy_version": None}
    policy = await session.get(Policy, version.policy_id)
    return {
        "policy_version_id": version.id,
        "policy_name": policy.name if policy else None,
        "policy_version": version.number,
    }


async def set_active_revision(
    session: AsyncSession, revision_id: str, robot_ids: list[str]
) -> None:
    """Record the revision robots acknowledged at a task boundary."""
    revision = await session.get(Revision, revision_id)
    if revision is None:
        return
    deployment = await session.get(Deployment, revision.deployment_id, with_for_update=True)
    if deployment is None:
        return
    now = iso(utcnow())
    active = dict(
        deployment.observed.get("active_revision_ids", dict.fromkeys(deployment.robot_ids))
    )
    running = await _policy_version_of(session, revision)
    for robot_id in robot_ids:
        active[robot_id] = revision_id
        robot = await session.get(Robot, robot_id, with_for_update=True)
        if robot is not None:
            robot.observed = {
                **robot.observed,
                "active_revision_id": revision_id,
                **running,
                "observed_at": now,
            }
            _touch(robot)
            await _emit(session, robot.project_id, robot)
    deployment.observed = {
        **deployment.observed,
        "active_revision_ids": active,
        "phase": "Active",
        "observed_at": now,
    }
    _touch(deployment)
    await _emit(session, deployment.project_id, deployment)


async def report_site(
    session: AsyncSession, site_id: str, *, connected: bool, agent_version: str | None
) -> None:
    site = await session.get(Site, site_id, with_for_update=True)
    if site is None:
        return
    site.observed = {
        **site.observed,
        "phase": "Ready" if connected else "Offline",
        "connected": connected,
        "agent_version": agent_version,
        "observed_at": iso(utcnow()),
    }
    _touch(site)
    await _emit(session, site.project_id, site)


async def report_site_ack(
    session: AsyncSession, site_id: str, *, generation: int, accepted: bool, reason: str | None
) -> None:
    """The agent's verdict on a desired-state generation: committed, or refused and why."""
    site = await session.get(Site, site_id, with_for_update=True)
    if site is None:
        return
    site.observed = {
        **site.observed,
        "committed_generation": generation,
        "manifest_accepted": accepted,
        "manifest_reason": reason,
        "observed_at": iso(utcnow()),
    }
    _touch(site)
    await _emit(session, site.project_id, site)


# A connected site reports status every few seconds (agent default 2 s, at most 60 s). A site whose
# last report is older than this is shown disconnected even if no stream end was recorded, e.g.
# after the control-plane process serving its stream crashed.
SITE_LIVENESS_S = 90.0


async def report_site_heartbeat(
    session: AsyncSession, site_id: str, *, offline_authorization_remaining_s: int
) -> None:
    """Periodic site status. Refreshes ``observed_at`` (from which reads derive liveness) on every
    report, but changes the version and emits an event only when a fact changed, so a status every
    few seconds does not flood the event stream."""
    site = await session.get(Site, site_id, with_for_update=True)
    if site is None:
        return
    now = utcnow()
    previous = site.observed.get("offline_authorization_expires_at")
    expires_at: str | None = None
    if site.observed.get("committed_generation"):  # nothing is authorized before a commit
        # The agent counts down from its last contact; the cloud records the instant. While
        # connected the expiry slides forward; small slides are the same fact as before.
        remaining = max(0, offline_authorization_remaining_s)
        expires = now + timedelta(seconds=remaining)
        jitter = timedelta(seconds=max(60.0, remaining / 20))
        same = previous is not None and abs(datetime.fromisoformat(previous) - expires) <= jitter
        expires_at = previous if same else iso(expires)
    changed = expires_at != previous
    site.observed = {
        **site.observed,
        "offline_authorization_expires_at": expires_at,
        "observed_at": iso(now),
    }
    if changed:
        _touch(site)
        await _emit(session, site.project_id, site)


async def report_capture(session: AsyncSession, site_id: str, summary: dict[str, Any]) -> None:
    """The site's episode store, counts only (design §28.2): on the site, and per deployment
    over its revisions. Versions change, and events go out, only when a count changed."""
    site = await session.get(Site, site_id, with_for_update=True)
    if site is None:
        return
    capture = {
        key: summary[key]
        for key in ("enabled", "episodes", "bytes", "steps", "outcomes", "datasets")
    }
    if site.observed.get("capture") != capture:
        site.observed = {**site.observed, "capture": capture}
        _touch(site)
        await _emit(session, site.project_id, site)
    by_revision: dict[str, dict[str, int]] = summary.get("revisions", {})
    deployments = (
        await session.scalars(
            select(Deployment).where(Deployment.site_id == site_id).with_for_update()
        )
    ).all()
    for deployment in deployments:
        revision_ids = (
            await session.scalars(
                select(Revision.id).where(Revision.deployment_id == deployment.id)
            )
        ).all()
        outcomes: dict[str, int] = {}
        for revision_id in revision_ids:
            for outcome, count in by_revision.get(revision_id, {}).items():
                outcomes[outcome] = outcomes.get(outcome, 0) + count
        counts = {"episodes": sum(outcomes.values()), "outcomes": outcomes}
        if deployment.observed.get("capture", {"episodes": 0, "outcomes": {}}) != counts:
            deployment.observed = {**deployment.observed, "capture": counts}
            _touch(deployment)
            await _emit(session, deployment.project_id, deployment)


async def report_server(session: AsyncSession, server_id: str, observed: dict[str, Any]) -> None:
    server = await session.get(Server, server_id, with_for_update=True)
    if server is None:
        return
    server.observed = {**server.observed, **observed, "observed_at": iso(utcnow())}
    _touch(server)
    await _emit(session, server.project_id, server)


async def report_robot(
    session: AsyncSession, robot_id: str, *, phase: str, reason: str | None = None, **extra: Any
) -> None:
    robot = await session.get(Robot, robot_id, with_for_update=True)
    if robot is None:
        return
    robot.observed = {
        **robot.observed,
        "phase": phase,
        "reason": reason,
        **extra,
        "observed_at": iso(utcnow()),
    }
    _touch(robot)
    await _emit(session, robot.project_id, robot)


async def upsert_session(
    session: AsyncSession,
    *,
    session_id: str,
    robot_id: str,
    revision_id: str,
    lease_generation: int,
    task_epoch: int,
    state: str,
    reason: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> RobotSession:
    robot = await session.get(Robot, robot_id)
    if robot is None:
        raise LookupError(robot_id)
    row = await session.get(RobotSession, session_id, with_for_update=True)
    now = utcnow()
    if row is None:
        row = RobotSession(
            id=session_id,
            project_id=robot.project_id,
            robot_id=robot.id,
            site_id=robot.site_id,
            revision_id=revision_id,
            lease_generation=lease_generation,
            task_epoch=task_epoch,
            state=state,
            reason=reason,
            metrics=metrics or {},
            observed_at=now,
        )
        session.add(row)
        await session.flush()
        await _emit(session, robot.project_id, row, "resource.created")
        return row
    if row.robot_id != robot.id or row.site_id != robot.site_id:
        # Session ids come from the reporting site: one never updates another robot's session.
        raise LookupError(session_id)
    row.task_epoch, row.state, row.reason = task_epoch, state, reason
    row.metrics = metrics or row.metrics
    row.observed_at = now
    _touch(row)
    await _emit(session, robot.project_id, row)
    return row


async def raise_fault(
    session: AsyncSession,
    *,
    robot_id: str,
    reason: str,
    message: str = "",
    latched: bool = True,
    local_response: str | None = None,
    session_id: str | None = None,
    revision_id: str | None = None,
    occurred_at: datetime | None = None,
    trace_ids: list[str] | None = None,
) -> Fault:
    robot = await session.get(Robot, robot_id)
    if robot is None:
        raise LookupError(robot_id)
    fault = Fault(
        id=new_id(Kind.FAULT),
        project_id=robot.project_id,
        robot_id=robot.id,
        session_id=session_id,
        revision_id=revision_id,
        reason=reason,
        message=message,
        latched=latched,
        local_response=local_response,
        occurred_at=occurred_at or utcnow(),
        trace_ids=trace_ids or [],
    )
    session.add(fault)
    await session.flush()
    await records.event(
        session, project_id=robot.project_id, type="fault.raised", resource=fault.id, version=1
    )
    return fault


async def clear_fault(session: AsyncSession, fault_id: str) -> None:
    fault = await session.get(Fault, fault_id, with_for_update=True)
    if fault is None or fault.cleared_at is not None:
        return
    fault.cleared_at = utcnow()
    _touch(fault)
    await records.event(
        session,
        project_id=fault.project_id,
        type="fault.cleared",
        resource=fault.id,
        version=fault.version,
    )


async def record_metrics(
    session: AsyncSession, *, project_id: str, site_id: str, points: list[dict[str, Any]]
) -> None:
    for point in points:
        session.add(
            MetricPoint(
                project_id=project_id,
                site_id=site_id,
                robot_id=point.get("robot_id"),
                revision_id=point.get("revision_id"),
                metric=point["metric"],
                ts=point["ts"],
                value=float(point["value"]),
            )
        )
