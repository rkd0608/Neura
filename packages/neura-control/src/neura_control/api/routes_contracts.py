"""Registered contract documents and compatibility checks (design §4)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import (
    ContractError,
    ExecutionProfile,
    ReasonCode,
    check_compatibility,
    parse_document,
    signing,
)
from neura_control.api import serializers
from neura_control.api.auth import authenticate
from neura_control.api.common import (
    ApiResult,
    idempotent,
    page_query,
    paginate,
    parse_body,
    resource_result,
    transaction,
)
from neura_control.api.schemas import (
    ArtifactSignatureCreate,
    CompatibilityCheckRequest,
    ContractCreate,
)
from neura_control.api.scope import Ctx, touch
from neura_control.db.models import ContractDocument, Project
from neura_control.ids import Kind, new_id
from neura_control.problems import ProblemError
from neura_control.security.principal import Permission, Principal
from neura_control.services.revisions import report_json

router = APIRouter()

# Audit actions are snake_case, like every other action (`deployment.update`, ...).
_ACTION_NAMES = {
    "ModelArtifact": "model_artifact",
    "RobotProfile": "robot_profile",
    "ExecutionProfile": "execution_profile",
}

COLLECTIONS: dict[str, tuple[str, Kind]] = {
    "robot-profiles": ("RobotProfile", Kind.ROBOT_PROFILE),
    "model-artifacts": ("ModelArtifact", Kind.MODEL_ARTIFACT),
    "execution-profiles": ("ExecutionProfile", Kind.EXECUTION_PROFILE),
}


async def load_contract(session: AsyncSession, project_id: str, identifier: str, kind: str) -> Any:
    row = await session.get(ContractDocument, identifier)
    if row is None or row.project_id != project_id or row.kind != kind:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED,
            f"{identifier} is not a registered {kind} in this project",
        )
    return parse_document(row.document)


def _register(collection: str) -> None:
    kind, id_kind = COLLECTIONS[collection]

    async def list_documents(
        project_id: str, request: Request, principal: Principal = Depends(authenticate)
    ) -> Response:
        query = page_query(request)
        async with transaction(request) as session:
            await Ctx(request, principal).project(session, project_id, Permission.READ)
            statement = select(ContractDocument).where(
                ContractDocument.project_id == project_id, ContractDocument.kind == kind
            )
            page = await paginate(
                session, statement, ContractDocument.id, query, serializers.contract
            )
        return ApiResult(200, page).response()

    async def register_document(
        project_id: str, request: Request, principal: Principal = Depends(authenticate)
    ) -> Response:
        body = await parse_body(request, ContractCreate)
        try:
            document = parse_document(body.document)
        except ContractError as exc:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED, str(exc), errors=exc.errors
            ) from exc
        if document.kind != kind:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED, f"expected a {kind} document, got {document.kind}"
            )
        digest = document.digest()
        if body.signatures is not None and kind != "ModelArtifact":
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED, "only model artifacts carry signatures"
            )
        signatures = _valid_signatures(digest, body.signatures or [])
        ctx = Ctx(request, principal)

        async def work(session: AsyncSession) -> ApiResult:
            project = await ctx.project(session, project_id, Permission.DEPLOY)
            existing = (
                await session.scalars(
                    select(ContractDocument).where(
                        ContractDocument.project_id == project.id,
                        ContractDocument.kind == kind,
                        ContractDocument.digest == digest,
                    )
                )
            ).first()
            if existing is not None:
                added = signatures and await _add_signatures(
                    session, ctx, project, existing, signatures
                )
                if added:
                    await session.flush()
                    await session.refresh(existing)
                return resource_result(200, serializers.contract(existing), existing.version)
            if isinstance(document, ExecutionProfile):
                await _check_binding_targets(session, project.id, document)
            row = ContractDocument(
                id=new_id(id_kind),
                project_id=project.id,
                kind=kind,
                digest=digest,
                document=document.canonical_data(),
                signatures=signatures,
                created_by=principal.subject,
            )
            session.add(row)
            await session.flush()
            await ctx.record(
                session,
                project,
                f"{_ACTION_NAMES[kind]}.register",
                row.id,
                version=1,
                event_type="resource.created",
            )
            await session.refresh(row)
            return resource_result(201, serializers.contract(row), 1, f"/v1/{collection}/{row.id}")

        return await idempotent(request, principal, work)

    async def get_document(
        identifier: str, request: Request, principal: Principal = Depends(authenticate)
    ) -> Response:
        async with transaction(request) as session:
            row, _ = await Ctx(request, principal).owned(
                session, ContractDocument, identifier, Permission.READ
            )
            if row.kind != kind:
                raise ProblemError(ReasonCode.API_NOT_FOUND)
            return resource_result(200, serializers.contract(row), row.version).response()

    singular = collection.removesuffix("s").replace("-", "_")
    router.add_api_route(
        f"/v1/projects/{{project_id}}/{collection}", list_documents, methods=["GET"]
    )
    router.add_api_route(
        f"/v1/projects/{{project_id}}/{collection}",
        register_document,
        methods=["POST"],
        status_code=201,
    )
    router.add_api_route(
        f"/v1/{collection}/{{{singular}_id}}",
        _rename(get_document, f"{singular}_id"),
        methods=["GET"],
    )


def _valid_signatures(digest: str, signatures: list[dict[str, Any]]) -> list[dict[str, str]]:
    try:
        return signing.validate(digest, signatures)
    except signing.SignatureError as exc:
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, str(exc)) from exc


async def _add_signatures(
    session: AsyncSession,
    ctx: Ctx,
    project: Project,
    row: ContractDocument,
    signatures: list[dict[str, str]],
) -> bool:
    """Merge signatures into a registered artifact (one per key). Returns whether it changed."""
    merged = signing.validate(row.digest, [*row.signatures, *signatures])
    if merged == row.signatures:
        return False
    row.signatures = merged
    touch(row)
    await ctx.record(
        session,
        project,
        "model_artifact.sign",
        row.id,
        version=row.version,
        event_type="resource.updated",
    )
    return True


@router.post("/v1/model-artifacts/{model_artifact_id}/signatures")
async def add_model_artifact_signature(
    model_artifact_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    """Add a publisher's signature over the artifact's digest (design §17.3). The document and
    its digest do not change; sites run it only if a key they trust signed it."""
    body = await parse_body(request, ArtifactSignatureCreate)
    ctx = Ctx(request, principal)

    async def work(session: AsyncSession) -> ApiResult:
        row, project = await ctx.owned(
            session, ContractDocument, model_artifact_id, Permission.DEPLOY, lock=True
        )
        if row.kind != "ModelArtifact":
            raise ProblemError(ReasonCode.API_NOT_FOUND)
        signature = _valid_signatures(row.digest, [body.model_dump()])
        if await _add_signatures(session, ctx, project, row, signature):
            await session.flush()
            await session.refresh(row)
        return resource_result(200, serializers.contract(row), row.version)

    return await idempotent(request, principal, work)


def _rename(handler: Any, param: str) -> Any:
    """Expose ``identifier`` under the path parameter name the spec uses."""

    async def wrapper(request: Request, principal: Principal = Depends(authenticate)) -> Response:
        response: Response = await handler(request.path_params[param], request, principal)
        return response

    wrapper.__name__ = handler.__name__
    return wrapper


async def _check_binding_targets(
    session: AsyncSession, project_id: str, profile: ExecutionProfile
) -> None:
    binding = profile.spec.binding
    for digest, kind in (
        (binding.model_artifact, "ModelArtifact"),
        (binding.robot_profile, "RobotProfile"),
    ):
        found = (
            await session.scalars(
                select(ContractDocument.id).where(
                    ContractDocument.project_id == project_id,
                    ContractDocument.kind == kind,
                    ContractDocument.digest == digest,
                )
            )
        ).first()
        if found is None:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED,
                f"the profile is bound to {kind} {digest}, which is not registered in this project",
            )


for _collection in COLLECTIONS:
    _register(_collection)


@router.post("/v1/projects/{project_id}/compatibility-checks")
async def check(
    project_id: str, request: Request, principal: Principal = Depends(authenticate)
) -> Response:
    body = await parse_body(request, CompatibilityCheckRequest)
    async with transaction(request) as session:
        await Ctx(request, principal).project(session, project_id, Permission.READ)
        artifact = await load_contract(session, project_id, body.model_artifact_id, "ModelArtifact")
        robot = await load_contract(session, project_id, body.robot_profile_id, "RobotProfile")
        profile = await load_contract(
            session, project_id, body.execution_profile_id, "ExecutionProfile"
        )
    return ApiResult(200, report_json(check_compatibility(artifact, robot, profile))).response()
