"""Shared API plumbing: transactions, idempotency, ETags, pagination, bodies, serialization."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neura_contracts import ReasonCode
from neura_control.db.models import IdempotencyKey
from neura_control.problems import ProblemError
from neura_control.security.principal import Principal

T = TypeVar("T", bound=BaseModel)
M = TypeVar("M")

MAX_BODY_BYTES = 4 * 1024 * 1024


@dataclass(slots=True)
class ApiResult:
    status: int
    body: dict[str, Any] | None
    headers: dict[str, str] = field(default_factory=dict)

    def response(self) -> Response:
        if self.body is None:
            return Response(status_code=self.status, headers=self.headers)
        return JSONResponse(self.body, status_code=self.status, headers=self.headers)


def etag(version: int) -> str:
    return f'"{version}"'


def resource_result(
    status: int, body: dict[str, Any], version: int, location: str | None = None
) -> ApiResult:
    headers = {"ETag": etag(version)}
    if location is not None:
        headers["Location"] = location
    return ApiResult(status, body, headers)


# --- transactions and idempotency --------------------------------------------------------------


@asynccontextmanager
async def transaction(request: Request) -> AsyncIterator[AsyncSession]:
    sessions: async_sessionmaker[AsyncSession] = request.app.state.sessions
    async with sessions.begin() as session:
        yield session


async def idempotent(
    request: Request,
    principal: Principal,
    work: Callable[[AsyncSession], Awaitable[ApiResult]],
) -> Response:
    """Run a POST exactly once per (principal, Idempotency-Key) (design §20.4).

    The key row is written in the same transaction as the change, so a crash never leaves a
    recorded key without its effect. Errors are not recorded: a retry re-evaluates.
    """
    key = request.headers.get("Idempotency-Key")
    if key is None:
        raise ProblemError(ReasonCode.API_IDEMPOTENCY_KEY_REQUIRED)
    if not 16 <= len(key) <= 128:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, "Idempotency-Key must be 16-128 characters"
        )
    body_hash = hashlib.sha256(await request.body()).hexdigest()
    path = request.url.path
    async with transaction(request) as session:
        inserted = await session.execute(
            insert(IdempotencyKey)
            .values(
                principal=principal.subject,
                key=key,
                method=request.method,
                path=path,
                body_hash=body_hash,
            )
            .on_conflict_do_nothing()
            .returning(IdempotencyKey.key)
        )
        if inserted.first() is None:
            stored = await session.get(IdempotencyKey, (principal.subject, key))
            assert stored is not None
            if (
                stored.body_hash != body_hash
                or stored.path != path
                or stored.method != request.method
            ):
                raise ProblemError(ReasonCode.API_IDEMPOTENCY_KEY_REUSED)
            assert stored.status_code is not None
            return ApiResult(
                stored.status_code, stored.response, dict(stored.response_headers or {})
            ).response()
        result = await work(session)
        row = await session.get(IdempotencyKey, (principal.subject, key))
        assert row is not None
        row.status_code = result.status
        row.response = result.body
        row.response_headers = result.headers
    return result.response()


def if_match(request: Request) -> int:
    header = request.headers.get("If-Match")
    if header is None:
        raise ProblemError(ReasonCode.API_PRECONDITION_REQUIRED)
    value = header.strip().removeprefix("W/").strip('"')
    try:
        return int(value)
    except ValueError as exc:
        raise ProblemError(
            ReasonCode.API_PRECONDITION_FAILED, "If-Match is not a known version"
        ) from exc


def check_version(current: int, expected: int) -> None:
    if current != expected:
        raise ProblemError(
            ReasonCode.API_PRECONDITION_FAILED, f"resource is at version {current}, not {expected}"
        )


# --- bodies ------------------------------------------------------------------------------------


async def read_json(request: Request) -> Any:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, "request body is too large")
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError as exc:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, f"body is not valid JSON: {exc}"
        ) from exc


async def parse_body[T: BaseModel](
    request: Request, model: type[T], *, merge_patch: bool = False
) -> T:
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    expected = "application/merge-patch+json" if merge_patch else "application/json"
    if content_type != expected:
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, f"Content-Type must be {expected}")
    return validate(model, await read_json(request))


def validate[T: BaseModel](model: type[T], data: Any) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in err['loc']) or '<body>'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, errors=errors) from exc


def apply_merge_patch(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """RFC 7396 JSON Merge Patch."""
    result = dict(target)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = apply_merge_patch(result[key], value)
        else:
            result[key] = value
    return result


# --- pagination --------------------------------------------------------------------------------


def encode_cursor(last_id: str) -> str:
    return base64.urlsafe_b64encode(last_id.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, "invalid cursor") from exc


@dataclass(frozen=True, slots=True)
class PageQuery:
    cursor: str | None
    page_size: int
    labels: dict[str, str]


def page_query(request: Request) -> PageQuery:
    params = request.query_params
    try:
        size = int(params.get("page_size", "50"))
    except ValueError as exc:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, "page_size must be an integer"
        ) from exc
    if not 1 <= size <= 200:
        raise ProblemError(ReasonCode.API_VALIDATION_FAILED, "page_size must be between 1 and 200")
    labels: dict[str, str] = {}
    for item in params.getlist("label"):
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED, "label filters look like key=value"
            )
        labels[key] = value
    return PageQuery(decode_cursor(params.get("cursor")), size, labels)


async def paginate(
    session: AsyncSession,
    statement: Select[Any],
    id_column: Any,
    query: PageQuery,
    serialize: Callable[[Any], dict[str, Any]],
    labels_column: Any = None,
) -> dict[str, Any]:
    if query.cursor is not None:
        statement = statement.where(id_column > query.cursor)
    if query.labels:
        if labels_column is None:
            raise ProblemError(
                ReasonCode.API_VALIDATION_FAILED, "this list does not support labels"
            )
        statement = statement.where(labels_column.contains(query.labels))
    rows = list(
        (await session.scalars(statement.order_by(id_column).limit(query.page_size + 1))).all()
    )
    more = len(rows) > query.page_size
    rows = rows[: query.page_size]
    next_cursor = encode_cursor(getattr(rows[-1], id_column.key)) if more and rows else None
    return {"items": [serialize(r) for r in rows], "page": {"next_cursor": next_cursor}}


async def get_or_404[M](
    session: AsyncSession, model: type[M], identifier: str, *, lock: bool = False
) -> M:
    statement = select(model).where(model.id == identifier)  # type: ignore[attr-defined]
    if lock:
        statement = statement.with_for_update()
    row = (await session.scalars(statement)).first()
    if row is None:
        raise ProblemError(ReasonCode.API_NOT_FOUND)
    return row
