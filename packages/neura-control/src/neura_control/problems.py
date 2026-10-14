"""RFC 9457 problem details carrying Neura reason codes (design §20.5)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from neura_contracts import REASON_CODES, ReasonCode

log = logging.getLogger("neura.control")

PROBLEM_MEDIA_TYPE = "application/problem+json"
# A URN rather than a URL: the docs domain is not decided yet (design §26.6).
PROBLEM_TYPE_BASE = "urn:neura:reason-code:"

_STATUS_TO_CODE: dict[int, ReasonCode] = {
    401: ReasonCode.API_UNAUTHENTICATED,
    403: ReasonCode.API_FORBIDDEN,
    404: ReasonCode.API_NOT_FOUND,
    405: ReasonCode.API_NOT_FOUND,
    409: ReasonCode.API_CONFLICT,
    412: ReasonCode.API_PRECONDITION_FAILED,
    422: ReasonCode.API_VALIDATION_FAILED,
    428: ReasonCode.API_IDEMPOTENCY_KEY_REQUIRED,
    429: ReasonCode.API_RATE_LIMITED,
    503: ReasonCode.API_UNAVAILABLE,
}


class ProblemError(Exception):
    """Raise from a handler to return a problem response for ``code``."""

    def __init__(
        self, code: ReasonCode, detail: str | None = None, *, errors: list[str] | None = None
    ) -> None:
        super().__init__(detail or REASON_CODES[code].message)
        self.code = code
        self.detail = detail
        self.errors = errors


def problem_response(
    code: ReasonCode,
    *,
    detail: str | None = None,
    instance: str | None = None,
    status: int | None = None,
    errors: list[str] | None = None,
) -> JSONResponse:
    info = REASON_CODES[code]
    http_status = status or info.http_status or 500
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE_BASE + code.value,
        "title": info.message,
        "status": http_status,
        "reason": code.value,
        "retryable": info.retryable,
    }
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=http_status, media_type=PROBLEM_MEDIA_TYPE)


async def handle_problem(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)
    return problem_response(
        exc.code, detail=exc.detail, instance=request.url.path, errors=exc.errors
    )


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_TO_CODE.get(exc.status_code, ReasonCode.API_INTERNAL_ERROR)
    detail = exc.detail if isinstance(exc.detail, str) else None
    return problem_response(code, detail=detail, instance=request.url.path, status=exc.status_code)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', 'invalid')}"
        for err in exc.errors()
    ]
    return problem_response(
        ReasonCode.API_VALIDATION_FAILED, instance=request.url.path, errors=errors
    )


async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    # Never leak internal details; the request id in logs ties the response to the cause.
    log.error(
        "unhandled error on %s %s (request %s)",
        request.method,
        request.url.path,
        getattr(request.state, "request_id", "-"),
        exc_info=exc,
    )
    return problem_response(ReasonCode.API_INTERNAL_ERROR, instance=request.url.path)
