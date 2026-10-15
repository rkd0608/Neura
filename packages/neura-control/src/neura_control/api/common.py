"""Shared API plumbing: transactions, idempotency, ETags, pagination, bodies, serialization."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
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


class _ResponseBox:
    """Seals a stored response under a key derived from the client's Idempotency-Key.

    Only the key's SHA-256 is stored, so the response (which can hold a secret shown once, such as
    a new API token or enrollment token) can be opened only by a retry that presents the same key,
    never by someone reading the database. Clients should use random keys; the SDK and the console
    use 128 random bits.
    """

    def __init__(self, key: str, principal: str) -> None:
        derived = HKDF(
            algorithm=SHA256(),
            length=32,
            salt=None,
            info=b"neura-idempotency-v1|" + principal.encode(),
        ).derive(key.encode())
        self._aead = AESGCM(derived)

    def seal(self, value: dict[str, Any]) -> dict[str, Any]:
        nonce = os.urandom(12)
        sealed = self._aead.encrypt(nonce, json.dumps(value).encode(), None)
        return {"sealed": base64.b64encode(nonce + sealed).decode()}

    def open(self, stored: dict[str, Any]) -> dict[str, Any]:
        raw = base64.b64decode(stored["sealed"])
        try:
            value: dict[str, Any] = json.loads(self._aead.decrypt(raw[:12], raw[12:], None))
        except InvalidTag as exc:  # a key whose hash collides, or a tampered row
            raise ProblemError(ReasonCode.API_IDEMPOTENCY_KEY_REUSED) from exc
        return value


async def idempotent(
    request: Request,
    principal: Principal,
    work: Callable[[AsyncSession], Awaitable[ApiResult]],
) -> Response:
    """Run a POST exactly once per (principal, Idempotency-Key) (design §20.4).

    The key row is written in the same transaction as the change, so a crash never leaves a
    recorded key without its effect. Errors are not recorded: a retry re-evaluates. The row holds
    the key's hash and the response sealed with the key itself (``_ResponseBox``).
    """
    key = request.headers.get("Idempotency-Key")
    if key is None:
        raise ProblemError(ReasonCode.API_IDEMPOTENCY_KEY_REQUIRED)
    if not 16 <= len(key) <= 128:
        raise ProblemError(
            ReasonCode.API_VALIDATION_FAILED, "Idempotency-Key must be 16-128 characters"
        )
    body_hash = hashlib.sha256(await bounded_body(request)).hexdigest()
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    box = _ResponseBox(key, principal.subject)
    path = request.url.path
    async with transaction(request) as session:
        inserted = await session.execute(
            insert(IdempotencyKey)
            .values(
                principal=principal.subject,
                key=key_hash,
                method=request.method,
                path=path,
                body_hash=body_hash,
            )
            .on_conflict_do_nothing()
            .returning(IdempotencyKey.key)
        )
        if inserted.first() is None:
            stored = await session.get(IdempotencyKey, (principal.subject, key_hash))
            assert stored is not None
            if (
                stored.body_hash != body_hash
                or stored.path != path
                or stored.method != request.method
            ):
                raise ProblemError(ReasonCode.API_IDEMPOTENCY_KEY_REUSED)
            assert stored.status_code is not None
            assert stored.response is not None
            replay = box.open(stored.response)
            return ApiResult(stored.status_code, replay["body"], replay["headers"]).response()
        result = await work(session)
        row = await session.get(IdempotencyKey, (principal.subject, key_hash))
        assert row is not None
        row.status_code = result.status
        row.response = box.seal({"body": result.body, "headers": result.headers})
        row.response_headers = {}
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


async def bounded_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """The request body, refused with 413 as soon as it is known to exceed ``limit``: from its
    Content-Length before anything is read, or while reading a body sent without one."""
    cached: bytes | None = getattr(request, "_body", None)
    if cached is not None:
        return cached
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ProblemError(ReasonCode.API_PAYLOAD_TOO_LARGE, f"the limit is {limit} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ProblemError(ReasonCode.API_PAYLOAD_TOO_LARGE, f"the limit is {limit} bytes")
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body  # Starlette's cache: later reads of the body reuse it
    return body


async def read_json(request: Request) -> Any:
    raw = await bounded_body(request)
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
