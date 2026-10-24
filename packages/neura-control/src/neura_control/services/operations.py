"""Starting site-dependent work: an Operation and the command its site receives (design §20.4).

API handlers and the rollout driver both start operations here; only a site acknowledgement moves
one forward (``site_reports.update_operation``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from neura_control.db.models import Operation, Project, SiteCommand
from neura_control.ids import Kind, new_id
from neura_control.security.principal import Principal
from neura_control.services import records


async def start(
    session: AsyncSession,
    project: Project,
    principal: Principal,
    *,
    request_id: str,
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
        created_by=principal.subject,
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
    await records.audit(
        session,
        principal=principal,
        organization_id=project.organization_id,
        project_id=project.id,
        action=f"operation.{kind}",
        target=operation.id,
        request_id=request_id,
    )
    await records.event(
        session,
        project_id=project.id,
        type="operation.updated",
        resource=operation.id,
        version=1,
    )
    await session.refresh(operation)
    return operation
