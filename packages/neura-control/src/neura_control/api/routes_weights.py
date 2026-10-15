"""Neura's registry of sealed weights (design §18.1, §18.2): upload, list, inspect, delete.

Only sealed files are accepted (``neura-contracts seal``). The control plane cannot open them;
sites fetch them over their authenticated channel and open them with the customer's key.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from neura_contracts import ReasonCode
from neura_control.api.auth import authenticate
from neura_control.api.common import (
    ApiResult,
    check_version,
    if_match,
    page_query,
    paginate,
    resource_result,
    transaction,
)
from neura_control.api.scope import Ctx
from neura_control.clock import iso
from neura_control.db.models import SealedWeights
from neura_control.problems import ProblemError
from neura_control.security.principal import Permission, Principal
from neura_control.services.sealed_weights import SealedUploadError, SealedWeightsStore

router = APIRouter()

OCTET_STREAM = "application/octet-stream"


def _store(request: Request) -> SealedWeightsStore:
    store: SealedWeightsStore | None = request.app.state.sealed_weights
    if store is None:
        raise ProblemError(
            ReasonCode.API_NOT_FOUND,
            "this control plane does not run a weight registry; keep weights in your own store "
            "and list it in the site's NEURA_AGENT_ARTIFACT_SOURCES",
        )
    return store


def _digest(digest: str) -> str:
    value = digest.removeprefix("sha256:")
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, f"not a sha256 digest: {digest}")
    return f"sha256:{value}"


def serialize(row: SealedWeights) -> dict[str, Any]:
    return {
        "project_id": row.project_id,
        "digest": row.digest,
        "key_id": row.key_id,
        "plaintext_bytes": row.plaintext_bytes,
        "sealed_bytes": row.sealed_bytes,
        "sealed_sha256": row.sealed_sha256,
        "version": str(row.version),
        "created_at": iso(row.created_at),
        "created_by": row.created_by,
    }


@router.put("/v1/projects/{project_id}/sealed-weights/{digest}")
async def upload_sealed_weights(
    project_id: str, digest: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    """Store a sealed weight file under the digest of its plaintext, replacing any earlier one
    (re-sealing with a new key, for instance)."""
    store = _store(request)
    digest = _digest(digest)
    if request.headers.get("content-type", "").split(";")[0].strip() != OCTET_STREAM:
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, f"send the file as {OCTET_STREAM}")
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        await ctx.project(session, project_id, Permission.DEPLOY)
    try:
        staged = await store.stage(project_id, digest, request.stream())
    except SealedUploadError as exc:
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, str(exc)) from exc
    stored = staged.info
    try:
        async with transaction(request) as session:
            project = await ctx.project(session, project_id, Permission.DEPLOY)
            inserted = await session.execute(
                insert(SealedWeights)
                .values(
                    project_id=project_id,
                    digest=digest,
                    key_id=stored.key_id,
                    plaintext_bytes=stored.plaintext_bytes,
                    sealed_bytes=stored.sealed_bytes,
                    sealed_sha256=stored.sealed_sha256,
                    version=1,
                    created_by=principal.subject,
                )
                .on_conflict_do_nothing()
                .returning(SealedWeights.digest)
            )
            created = inserted.first() is not None
            # The row lock orders concurrent uploads of one digest: file and row always agree.
            row = await session.get(
                SealedWeights, (project_id, digest), with_for_update=True, populate_existing=True
            )
            assert row is not None
            row.key_id = stored.key_id
            row.plaintext_bytes = stored.plaintext_bytes
            row.sealed_bytes = stored.sealed_bytes
            row.sealed_sha256 = stored.sealed_sha256
            row.created_by = principal.subject
            if not created:
                row.version += 1
            store.commit(staged)
            await ctx.record(session, project, "sealed_weights.upload", digest)
            await session.flush()
            await session.refresh(row)
            body = serialize(row)
    finally:
        store.discard(staged)  # a no-op once committed
    location = f"/v1/projects/{project_id}/sealed-weights/{digest}"
    if created:
        return resource_result(201, body, int(body["version"]), location).response()
    return resource_result(200, body, int(body["version"])).response()


@router.get("/v1/projects/{project_id}/sealed-weights")
async def list_sealed_weights(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    _store(request)
    query = page_query(request)
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        statement = select(SealedWeights).where(SealedWeights.project_id == project_id)
        page = await paginate(session, statement, SealedWeights.digest, query, serialize)
    return ApiResult(200, page).response()


@router.get("/v1/projects/{project_id}/sealed-weights/{digest}")
async def get_sealed_weights(
    project_id: str, digest: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    _store(request)
    digest = _digest(digest)
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        row = await session.get(SealedWeights, (project_id, digest))
        if row is None:
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        return resource_result(200, serialize(row), row.version).response()


@router.delete("/v1/projects/{project_id}/sealed-weights/{digest}", status_code=204)
async def delete_sealed_weights(
    project_id: str, digest: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    """Remove a sealed file. Sites that already opened it keep their copy; others can no longer
    fetch it."""
    store = _store(request)
    digest = _digest(digest)
    expected = if_match(request)
    ctx = Ctx(request, principal)
    async with transaction(request) as session:
        project = await ctx.project(session, project_id, Permission.DEPLOY)
        row = await session.get(SealedWeights, (project_id, digest), with_for_update=True)
        if row is None:
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        check_version(row.version, expected)
        await session.delete(row)
        # Under the row lock, like uploads: a concurrent upload of this digest waits for this
        # transaction and then writes its own file, which this delete can no longer remove.
        store.delete(project_id, digest)
        await ctx.record(session, project, "sealed_weights.delete", digest)
    return Response(status_code=204)
