"""Desired-state manifests for sites (design §17.3, §20.8).

A manifest is everything a site needs to operate without the cloud: its robots, its deployments,
and every live revision with the full contract documents. It is canonical JSON, signed, and its
generation increments exactly when its content changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import canonical_json, content_digest
from neura_contracts.site_protocol import MANIFEST_API_VERSION
from neura_control.db.models import ContractDocument, Deployment, Revision, Robot, Site
from neura_control.security.signing import ManifestSigner

LIVE_PHASES = ("Created", "Preparing", "Prepared")


@dataclass(frozen=True, slots=True)
class SignedManifest:
    generation: int
    payload: bytes
    signature: bytes
    key_id: str


async def manifest_content(session: AsyncSession, site: Site) -> dict[str, Any]:
    robots = (
        await session.scalars(select(Robot).where(Robot.site_id == site.id).order_by(Robot.id))
    ).all()
    deployments = (
        await session.scalars(
            select(Deployment).where(Deployment.site_id == site.id).order_by(Deployment.id)
        )
    ).all()
    # A revision robots are running stays in the manifest whatever its phase (a failed re-prepare,
    # say): dropping it would stop those robots, and only an activation may change what they run.
    active = {
        revision_id
        for d in deployments
        for revision_id in d.observed.get("active_revision_ids", {}).values()
        if revision_id
    }
    revisions = (
        await session.scalars(
            select(Revision)
            .where(
                Revision.deployment_id.in_([d.id for d in deployments]),
                or_(Revision.phase.in_(LIVE_PHASES), Revision.id.in_(active)),
            )
            .order_by(Revision.id)
        )
    ).all()
    document_ids = {
        doc_id
        for r in revisions
        for doc_id in (r.model_artifact_id, r.robot_profile_id, r.execution_profile_id)
    }
    documents = {
        d.id: d
        for d in (
            await session.scalars(
                select(ContractDocument).where(ContractDocument.id.in_(document_ids))
            )
        ).all()
    }

    def doc(identifier: str) -> dict[str, Any]:
        row = documents[identifier]
        return {"id": row.id, "digest": row.digest, "document": row.document}

    by_deployment: dict[str, list[dict[str, Any]]] = {}
    for revision in revisions:
        by_deployment.setdefault(revision.deployment_id, []).append(
            {
                "id": revision.id,
                "revision_digest": revision.revision_digest,
                "documents": {
                    "model_artifact": doc(revision.model_artifact_id),
                    "robot_profile": doc(revision.robot_profile_id),
                    "execution_profile": doc(revision.execution_profile_id),
                },
            }
        )
    return {
        "api_version": MANIFEST_API_VERSION,
        "site_id": site.id,
        "project_id": site.project_id,
        "offline_authorization_hours": site.offline_authorization_hours,
        "robots": [
            {"id": r.id, "name": r.name, "robot_profile_id": r.robot_profile_id} for r in robots
        ],
        "deployments": [
            {
                "id": d.id,
                "name": d.name,
                "robot_ids": d.robot_ids,
                "desired_revision_id": d.desired_revision_id,
                "rollout": d.rollout,
                "revisions": by_deployment.get(d.id, []),
            }
            for d in deployments
        ],
    }


async def current_manifest(
    session: AsyncSession, site_id: str, signer: ManifestSigner
) -> SignedManifest:
    """Build the site's manifest, bumping its generation if the content changed. The caller's
    transaction persists the new generation."""
    site = await session.get(Site, site_id, with_for_update=True)
    if site is None:
        raise LookupError(site_id)
    content = await manifest_content(session, site)
    digest = content_digest(content)
    if digest != site.manifest_digest:
        site.manifest_generation += 1
        site.manifest_digest = digest
    payload = canonical_json({**content, "generation": site.manifest_generation})
    return SignedManifest(site.manifest_generation, payload, signer.sign(payload), signer.key_id)
