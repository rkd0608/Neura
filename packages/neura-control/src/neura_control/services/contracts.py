"""Registering contract documents from inside the control plane: the execution profiles a rollout
derives and the robot profiles a site's agent reports (design §29). Documents are immutable and
deduplicated by digest, as when registered through the API."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import parse_document
from neura_contracts.documents import Document
from neura_control.db.models import ContractDocument
from neura_control.ids import Kind, new_id

KINDS = {
    "RobotProfile": Kind.ROBOT_PROFILE,
    "ModelArtifact": Kind.MODEL_ARTIFACT,
    "ExecutionProfile": Kind.EXECUTION_PROFILE,
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
    """The registered row for ``document`` and whether it was new. Raises ``ContractError`` for
    an invalid document."""
    parsed = document if not isinstance(document, dict) else parse_document(document)
    digest = parsed.digest()
    existing = await find(session, project_id, parsed.kind, digest)
    if existing is not None:
        return existing, False
    row = ContractDocument(
        id=new_id(KINDS[parsed.kind]),
        project_id=project_id,
        kind=parsed.kind,
        digest=digest,
        document=parsed.canonical_data(),
        signatures=signatures or [],
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row, True
