"""Sessions, faults, metrics, the change-event stream and audit records."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select

from neura_contracts import ReasonCode
from neura_control.api import serializers
from neura_control.api.auth import authenticate
from neura_control.api.common import (
    ApiResult,
    page_query,
    paginate,
    parse_body,
    resource_result,
    transaction,
)
from neura_control.api.schemas import MetricsQuery
from neura_control.api.scope import Ctx
from neura_control.clock import iso
from neura_control.db.models import AuditRecord, Event, Fault, MetricPoint, RobotSession
from neura_control.problems import ProblemError, problem_response
from neura_control.security.principal import Permission, Principal
from neura_control.settings import ControlPlaneSettings

router = APIRouter()


@router.get("/v1/projects/{project_id}/sessions")
async def list_sessions(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        page = await paginate(
            session,
            select(RobotSession).where(RobotSession.project_id == project_id),
            RobotSession.id,
            query,
            serializers.session,
        )
    return ApiResult(200, page).response()


@router.get("/v1/sessions/{session_id}")
async def get_session(
    session_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        row, _ = await Ctx(request, principal).owned(
            session, RobotSession, session_id, Permission.READ
        )
        return resource_result(200, serializers.session(row), row.version).response()


@router.get("/v1/projects/{project_id}/faults")
async def list_faults(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        page = await paginate(
            session,
            select(Fault).where(Fault.project_id == project_id),
            Fault.id,
            query,
            serializers.fault,
        )
    return ApiResult(200, page).response()


@router.get("/v1/faults/{fault_id}")
async def get_fault(
    fault_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    async with transaction(request) as session:
        row, _ = await Ctx(request, principal).owned(session, Fault, fault_id, Permission.READ)
        return resource_result(200, serializers.fault(row), row.version).response()


@router.post("/v1/projects/{project_id}/metrics/query")
async def query_metrics(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, MetricsQuery)
    try:
        start = datetime.fromisoformat(str(body.start))
        end = datetime.fromisoformat(str(body.end))
    except ValueError as exc:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, "start and end must be RFC 3339 timestamps"
        ) from exc
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, "start and end need time zones and end > start"
        )
    step = timedelta(seconds=body.step_seconds)
    if (end - start) / step > 10_000:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, "too many points; increase step_seconds"
        )
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        bucket = func.date_bin(step, MetricPoint.ts, start).label("bucket")
        statement = (
            select(bucket, func.avg(MetricPoint.value))
            .where(
                MetricPoint.project_id == project_id,
                MetricPoint.metric == body.metric,
                MetricPoint.ts >= start,
                MetricPoint.ts < end,
            )
            .group_by(bucket)
            .order_by(bucket)
        )
        filters = body.filters
        for column, value in (
            (MetricPoint.site_id, filters.site_id),
            (MetricPoint.robot_id, filters.robot_id),
            (MetricPoint.revision_id, filters.revision_id),
        ):
            if value is not None:
                statement = statement.where(column == value)
        rows = {r[0]: float(r[1]) for r in (await session.execute(statement)).all()}
    points = []
    at = start
    while at < end:
        points.append([iso(at), rows.get(at)])
        at += step
    labels = {k: v for k, v in filters.model_dump().items() if v is not None}
    return ApiResult(
        200, {"metric": body.metric, "series": [{"labels": labels, "points": points}]}
    ).response()


@router.get("/v1/projects/{project_id}/audit-records")
async def list_audit(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        page = await paginate(
            session,
            select(AuditRecord).where(AuditRecord.project_id == project_id),
            AuditRecord.id,
            query,
            serializers.audit_record,
        )
    return ApiResult(200, page).response()


def _event_json(row: Event) -> dict[str, object]:
    data: dict[str, object] = {
        "cursor": str(row.cursor),
        "type": row.type,
        "resource": row.resource,
        "resource_version": row.resource_version,
        "occurred_at": iso(row.occurred_at),
    }
    if row.summary is not None:  # activity a person reads (routes_fleet.list_activity)
        data.update(
            summary=row.summary,
            level=row.level,
            site_id=row.site_id,
            robot_id=row.robot_id,
            rollout_id=row.rollout_id,
        )
    return data


@router.get("/v1/projects/{project_id}/events")
async def stream_events(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    """Server-sent events, resumable with ``Last-Event-ID`` or ``cursor`` (design §20.6)."""
    settings: ControlPlaneSettings = request.app.state.settings
    raw_cursor = request.headers.get("Last-Event-ID") or request.query_params.get("cursor")
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        latest = (
            await session.scalar(
                select(func.max(Event.cursor)).where(Event.project_id == project_id)
            )
            or 0
        )
        oldest = await session.scalar(
            select(func.min(Event.cursor)).where(Event.project_id == project_id)
        )
    streams: list[int] = request.app.state.event_streams
    if streams[0] >= settings.events_max_streams:
        limited = problem_response(ReasonCode.API_RATE_LIMITED, instance=request.url.path)
        limited.headers["Retry-After"] = "5"
        return limited
    if raw_cursor is None:
        position = latest
    else:
        try:
            position = int(raw_cursor)
        except ValueError as exc:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED, "cursor must be an event cursor"
            ) from exc
        if oldest is not None and position < oldest - 1:
            return problem_response(ReasonCode.API_CURSOR_EXPIRED, instance=request.url.path)

    async def generate() -> AsyncIterator[bytes]:
        streams[0] += 1
        try:
            async for chunk in _events(request, settings, project_id, position):
                yield chunk
        finally:
            streams[0] -= 1

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _events(
    request: Request, settings: ControlPlaneSettings, project_id: str, position: int
) -> AsyncIterator[bytes]:
    idle = 0.0
    deadline = asyncio.get_running_loop().time() + settings.events_max_stream_s
    # The starting position as an id-only message: a client that reconnects before any
    # event arrives resumes from here and misses nothing (SSE sets lastEventId from it).
    yield f"retry: 1000\nid: {position}\n\n".encode()
    while asyncio.get_running_loop().time() < deadline and not await request.is_disconnected():
        async with transaction(request) as session:
            rows = (
                await session.scalars(
                    select(Event)
                    .where(Event.project_id == project_id, Event.cursor > position)
                    .order_by(Event.cursor)
                    .limit(200)
                )
            ).all()
        for row in rows:
            position = row.cursor
            payload = json.dumps(_event_json(row), separators=(",", ":"))
            yield f"id: {row.cursor}\nevent: {row.type}\ndata: {payload}\n\n".encode()
        if rows:
            idle = 0.0
            continue
        await asyncio.sleep(settings.events_poll_interval_s)
        idle += settings.events_poll_interval_s
        if idle >= settings.events_keepalive_s:
            idle = 0.0
            yield b": keepalive\n\n"
