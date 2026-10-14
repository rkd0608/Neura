"""Audit records and change events, written in the same transaction as each change."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import ReasonCode
from neura_control.db.models import AuditRecord, Event
from neura_control.ids import Kind, new_id
from neura_control.security.principal import Principal


async def audit(
    session: AsyncSession,
    *,
    principal: Principal,
    organization_id: str,
    project_id: str | None,
    action: str,
    target: str,
    request_id: str,
    result: str = "succeeded",
    reason: ReasonCode | None = None,
) -> None:
    session.add(
        AuditRecord(
            id=new_id(Kind.AUDIT_RECORD),
            organization_id=organization_id,
            project_id=project_id,
            actor=principal.subject,
            actor_kind=principal.kind.value,
            action=action,
            target=target,
            result=result,
            reason=reason.value if reason else None,
            request_id=request_id,
        )
    )


async def event(
    session: AsyncSession,
    *,
    project_id: str,
    type: str,
    resource: str,
    version: int,
) -> None:
    session.add(
        Event(project_id=project_id, type=type, resource=resource, resource_version=str(version))
    )
