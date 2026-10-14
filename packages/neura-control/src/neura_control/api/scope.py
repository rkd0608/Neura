"""Resolving the organization and project a resource belongs to, with authorization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import ReasonCode
from neura_control.clock import utcnow
from neura_control.db.models import Organization, Project
from neura_control.problems import ProblemError
from neura_control.security.principal import Permission, Principal
from neura_control.services import records

M = TypeVar("M")


@dataclass(slots=True)
class Ctx:
    """Per-request context for handlers: who is calling and the request id for audit."""

    request: Request
    principal: Principal

    @property
    def request_id(self) -> str:
        return str(getattr(self.request.state, "request_id", ""))

    async def organization(
        self, session: AsyncSession, organization_id: str, permission: Permission
    ) -> Organization:
        org = await session.get(Organization, organization_id)
        if org is None:
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        self.principal.require(permission, org.id)
        return org

    async def project(
        self, session: AsyncSession, project_id: str, permission: Permission
    ) -> Project:
        project = await session.get(Project, project_id)
        if project is None:
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        self.principal.require(permission, project.organization_id, project.id)
        return project

    async def owned(
        self,
        session: AsyncSession,
        model: type[M],
        identifier: str,
        permission: Permission,
        *,
        lock: bool = False,
    ) -> tuple[M, Project]:
        """Load a project-scoped resource and authorize access to its project."""
        row = await session.get(model, identifier, with_for_update=lock)
        if row is None:
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        project = await self.project(session, row.project_id, permission)  # type: ignore[attr-defined]
        return row, project

    async def record(
        self,
        session: AsyncSession,
        project: Project,
        action: str,
        target: str,
        *,
        version: int | None = None,
        event_type: str | None = None,
    ) -> None:
        """Audit the change and emit a change event, in the caller's transaction."""
        await records.audit(
            session,
            principal=self.principal,
            organization_id=project.organization_id,
            project_id=project.id,
            action=action,
            target=target,
            request_id=self.request_id,
        )
        if event_type is not None and version is not None:
            await records.event(
                session, project_id=project.id, type=event_type, resource=target, version=version
            )


def touch(row: Any) -> None:
    """Bump the optimistic-concurrency version after a change."""
    row.version += 1
    row.updated_at = utcnow()
