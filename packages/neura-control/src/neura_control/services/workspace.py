"""Workspaces: an organization and the project its sites, robots and policies live in.

People and tools address a workspace by its organization; the project is an implementation
detail they never have to choose (design §29). An organization's oldest project is its default
one, created when the organization has none.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import ReasonCode
from neura_control.db.models import Membership, Organization, Project
from neura_control.ids import Kind, new_id
from neura_control.problems import ProblemError
from neura_control.security.principal import Permission, Principal

DEFAULT_PROJECT = "default"


async def organization(
    session: AsyncSession, principal: Principal, organization_id: str | None
) -> Organization:
    """The workspace a request is about: the one named, or the caller's only one."""
    if organization_id is None:
        visible = sorted(principal.grants)
        if not visible:
            raise ProblemError(ReasonCode.API_NOT_FOUND, "you are not a member of any workspace")
        if len(visible) > 1:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED,
                "you are in several workspaces: name one with organization_id ("
                + ", ".join(visible)
                + ")",
            )
        organization_id = visible[0]
    org = await session.get(Organization, organization_id)
    if org is None:
        raise ProblemError(ReasonCode.API_NOT_FOUND)
    principal.require(Permission.READ, org.id)
    return org


async def default_project(
    session: AsyncSession, principal: Principal, org: Organization
) -> Project:
    statement = select(Project).where(Project.organization_id == org.id).order_by(Project.id)
    if principal.project_ids is not None:
        statement = statement.where(Project.id.in_(principal.project_ids))
    project = (await session.scalars(statement.limit(1))).first()
    if project is not None:
        return project
    if principal.project_ids is not None:
        raise ProblemError(ReasonCode.API_NOT_FOUND, "this token reaches no project")
    project = Project(
        id=new_id(Kind.PROJECT),
        organization_id=org.id,
        name=DEFAULT_PROJECT,
        display_name=org.display_name,
        labels={},
        created_by="system",
    )
    try:
        async with session.begin_nested():
            session.add(project)
    except IntegrityError:  # created by a concurrent request
        project = (
            await session.scalars(
                select(Project)
                .where(Project.organization_id == org.id)
                .order_by(Project.id)
                .limit(1)
            )
        ).one()
    return project


async def create_personal(
    session: AsyncSession, *, subject: str, email: str | None, name: str | None
) -> Organization:
    """A workspace for someone who signed up on their own: they are its admin."""
    label = name or (email.split("@", 1)[0] if email else "My workspace")
    org = Organization(
        id=new_id(Kind.ORGANIZATION),
        display_name=f"{label}'s workspace"[:200],
        created_by=subject,
    )
    session.add(org)
    await session.flush()
    session.add(
        Membership(
            id=new_id(Kind.MEMBER),
            organization_id=org.id,
            subject=subject,
            email=email.lower() if email else None,
            role="admin",
            created_by=subject,
        )
    )
    session.add(
        Project(
            id=new_id(Kind.PROJECT),
            organization_id=org.id,
            name=DEFAULT_PROJECT,
            display_name=org.display_name,
            labels={},
            created_by=subject,
        )
    )
    await session.flush()
    return org
