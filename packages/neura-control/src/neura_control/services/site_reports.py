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

from neura_contracts import ReasonCode
from neura_control.clock import iso, utcnow
from neura_control.db.models import (
    ActivationRequest,
    Deployment,
    Fault,
    MetricPoint,
    Operation,
    Revision,
    Robot,
    RobotSession,
    Server,
    Site,
)
from neura_control.ids import Kind, new_id
from neura_control.services import records

log = logging.getLogger("neura.control")
TERMINAL = frozenset({"Succeeded", "Failed", "Cancelled"})


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
    for robot_id in robot_ids:
        active[robot_id] = revision_id
        robot = await session.get(Robot, robot_id, with_for_update=True)
        if robot is not None:
            robot.observed = {
                **robot.observed,
                "active_revision_id": revision_id,
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
