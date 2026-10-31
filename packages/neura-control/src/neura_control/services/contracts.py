"""Registering contract documents (design §4, §29): found or added by digest, with publisher
signatures merged one per key. ``record`` is how a person or a site registers or signs one, with
the audit record and change event in the same transaction (design §20.4); ``register`` alone is
for the documents the control plane derives itself or a site reports, whose callers say what
happened. Documents are immutable, and registering one twice, even at once, gives one row."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import parse_document, signing
from neura_contracts.documents import Document
from neura_control.db.models import ContractDocument, Project
from neura_control.ids import Kind, new_id
from neura_control.security.principal import Principal
from neura_control.services import records

KINDS = {
    "RobotProfile": Kind.ROBOT_PROFILE,
    "ModelArtifact": Kind.MODEL_ARTIFACT,
    "ExecutionProfile": Kind.EXECUTION_PROFILE,
}
# Audit actions are snake_case, like every other action (`deployment.update`, ...).
ACTIONS = {
    "RobotProfile": "robot_profile",
    "ModelArtifact": "model_artifact",
    "ExecutionProfile": "execution_profile",
}


async def find(
    session: AsyncSession, project_id: str, kind: str, digest: str
) -> ContractDocument | None:
    return (
        await session.scalars(
            select(ContractDocument).where(
                ContractDocument.project_id == project_id,
                ContractDocument.kind == kind,
                ContractDocument.digest == digest,
            )
        )
    ).first()


async def register(
    session: AsyncSession,
    project_id: str,
    document: dict[str, Any] | Document,
    *,
    created_by: str,
    signatures: list[dict[str, str]] | None = None,
) -> tuple[ContractDocument, bool]:
    """The registered row for ``document`` and whether it was new, with ``signatures`` (validated
    by the caller) if it was. Raises ``ContractError`` for an invalid document."""
    parsed = document if not isinstance(document, dict) else parse_document(document)
    digest = parsed.digest()
    existing = await find(session, project_id, parsed.kind, digest)
    if existing is not None:
        return existing, False
    # A transaction registering the same document at the same time makes this insert wait for
    # it; if it commits, nothing is inserted here and its row is read below.
    inserted = await session.scalar(
        insert(ContractDocument)
        .values(
            id=new_id(KINDS[parsed.kind]),
            project_id=project_id,
            kind=parsed.kind,
            digest=digest,
            document=parsed.canonical_data(),
            signatures=signatures or [],
            created_by=created_by,
        )
        .on_conflict_do_nothing(index_elements=["project_id", "kind", "digest"])
        .returning(ContractDocument.id)
    )
    row = await find(session, project_id, parsed.kind, digest)
    assert row is not None
    return row, inserted is not None


async def record(
    session: AsyncSession,
    project: Project,
    document: dict[str, Any] | Document,
    *,
    principal: Principal,
    request_id: str,
    signatures: Sequence[dict[str, str]] = (),
    created_by: str | None = None,
) -> tuple[ContractDocument, bool]:
    """Register ``document`` for ``principal``, adding ``signatures`` (validated against its
    digest by the caller) to it if it was registered already. Audits and announces what changed:
    ``<kind>.register`` for a new document, ``<kind>.sign`` for new signatures. Returns the row
    and whether it was new."""
    row, new = await register(
        session,
        project.id,
        document,
        created_by=created_by or principal.subject,
        signatures=list(signatures),
    )
    if new:
        await _announce(session, project, row, principal, request_id, "register")
    elif signatures:
        await session.refresh(row, with_for_update=True)  # signatures merge one at a time
        await sign(session, project, row, signatures, principal=principal, request_id=request_id)
    return row, new


async def sign(
    session: AsyncSession,
    project: Project,
    row: ContractDocument,
    signatures: Sequence[dict[str, str]],
    *,
    principal: Principal,
    request_id: str,
) -> bool:
    """Merge ``signatures`` into a registered document (one per key; the caller holds its row
    lock), auditing and announcing the change. Returns whether any was added."""
    merged = signing.validate(row.digest, [*row.signatures, *signatures])
    if merged == row.signatures:
        return False
    row.signatures = merged
    records.touch(row)
    await _announce(session, project, row, principal, request_id, "sign")
    return True


async def _announce(
    session: AsyncSession,
    project: Project,
    row: ContractDocument,
    principal: Principal,
    request_id: str,
    verb: str,
) -> None:
    await records.audit(
        session,
        principal=principal,
        organization_id=project.organization_id,
        project_id=project.id,
        action=f"{ACTIONS[row.kind]}.{verb}",
        target=row.id,
        request_id=request_id,
    )
    await records.event(
        session,
        project_id=project.id,
        type="resource.created" if verb == "register" else "resource.updated",
        resource=row.id,
        version=row.version,
    )
