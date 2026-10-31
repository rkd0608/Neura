"""The transport under the generated operations: authentication, idempotency, concurrency
preconditions, retries, errors, pagination, operations and the change-event stream."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import httpx

from neura_sdk._operations import Operations
from neura_sdk.models import Event, Operation, SealedWeights

# For a response without a problem body (a proxy's error page): the statuses worth repeating.
RETRYABLE_STATUS = frozenset({429, 503})
TERMINAL_OPERATION_STATES = frozenset({"Succeeded", "Failed", "Cancelled"})


class NeuraApiError(Exception):
    """An RFC 9457 problem from the API. ``reason`` is a code from the reason-code catalog."""

    def __init__(self, status: int, problem: dict[str, Any]) -> None:
        self.status = status
        self.problem = problem
        self.reason: str = str(problem.get("reason", "UNKNOWN"))
        self.title: str = str(problem.get("title", ""))
        self.detail: str = str(problem.get("detail", ""))
        self.retryable = _retryable(status, problem)
        self.errors: list[dict[str, Any]] = list(problem.get("errors", []))
        super().__init__(f"{status} {self.reason}: {self.detail or self.title}")


class OperationFailedError(Exception):
    def __init__(self, operation: Operation) -> None:
        self.operation = operation
        super().__init__(
            f"operation {operation['metadata']['id']} {operation['state']}: "
            f"{operation.get('reason')} {operation.get('message', '')}".strip()
        )


class Client(Operations):
    """Synchronous client. Methods return the API's JSON objects (typed as TypedDicts)."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        user_agent: str = "neura-sdk-python/0.1",
    ) -> None:
        headers = {"User-Agent": user_agent}
        if token:  # without one, only the public operations (signing in, metadata) answer
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport, headers=headers
        )
        self._max_retries = max_retries
        self._sleep: Callable[[float], None] = time.sleep

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # --- transport ------------------------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
        content_type: str | None = None,
        if_match: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        headers: dict[str, str] = {}
        if method == "POST":
            # One key for every attempt: a retried POST is applied at most once (design §20.4).
            headers["Idempotency-Key"] = idempotency_key or secrets.token_hex(16)
        if if_match is not None:
            headers["If-Match"] = if_match if if_match.startswith('"') else f'"{if_match}"'
        content = None
        if body is not None:
            content = json.dumps(body).encode()
            headers["Content-Type"] = content_type or "application/json"
        params = {k: v for k, v in (query or {}).items() if v is not None}
        attempt = 0
        while True:
            response = self._http.request(
                method, path, params=params, content=content, headers=headers
            )
            if response.status_code < 400:
                break
            error = NeuraApiError(response.status_code, _problem(response))
            if not error.retryable or attempt >= self._max_retries:
                raise error
            attempt += 1
            self._sleep(_retry_delay(response, attempt))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # --- helpers --------------------------------------------------------------------------

    def paginate(
        self, list_operation: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Iterator[Any]:
        """Iterate every item of a list operation, following cursors:
        ``for robot in client.paginate(client.list_robots, project_id): ...``"""
        cursor: str | None = None
        while True:
            page = list_operation(*args, cursor=cursor, **kwargs)
            yield from page["items"]
            cursor = page["page"].get("next_cursor")
            if not cursor:
                return

    def wait_for_operation(
        self, operation_id: str, *, timeout_s: float = 600.0, poll_s: float = 1.0
    ) -> Operation:
        """Wait until a site-dependent operation ends. Raises OperationFailedError unless it
        succeeded. Activations wait on a person at the site, so give them a generous timeout."""
        deadline = time.monotonic() + timeout_s
        while True:
            operation = self.get_operation(operation_id)
            if operation["state"] in TERMINAL_OPERATION_STATES:
                if operation["state"] != "Succeeded":
                    raise OperationFailedError(operation)
                return operation
            if time.monotonic() >= deadline:
                raise TimeoutError(f"operation {operation_id} is still {operation['state']}")
            self._sleep(poll_s)

    def events(self, project_id: str, *, last_event_id: str | None = None) -> Iterator[Event]:
        """Follow the project's change events (``GET /v1/projects/{project_id}/events``),
        reconnecting from the last cursor when the server ends a stream. Events say what
        changed; re-read the resource for its state."""
        cursor = last_event_id
        while True:
            headers = {"Accept": "text/event-stream"}
            if cursor:
                headers["Last-Event-ID"] = cursor
            path = f"/v1/projects/{quote(project_id, safe='')}/events"
            with self._http.stream("GET", path, headers=headers, timeout=None) as response:
                if response.status_code >= 400:
                    response.read()
                    raise NeuraApiError(response.status_code, _problem(response))
                for item in _parse_sse(response.iter_lines()):
                    if isinstance(item, str):
                        cursor = item
                        continue
                    cursor = item["cursor"]
                    yield item

    def upload_sealed_weights(self, project_id: str, path: str | Path) -> SealedWeights:
        """Upload one file sealed with ``neura-contracts seal`` (``PUT
        /v1/projects/{project_id}/sealed-weights/{digest}``). The sealed layout names each file
        by its plaintext digest, which the upload is stored under."""
        file = Path(path)
        digest = f"sha256:{file.name}"
        url = f"/v1/projects/{quote(project_id, safe='')}/sealed-weights/{quote(digest, safe='')}"
        with file.open("rb") as f:
            response = self._http.put(
                url,
                content=iter(lambda: f.read(1 << 20), b""),
                headers={"Content-Type": "application/octet-stream"},
                timeout=None,
            )
        if response.status_code >= 400:
            raise NeuraApiError(response.status_code, _problem(response))
        result: SealedWeights = response.json()
        return result


def _problem(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        return body
    return {"reason": "UNKNOWN", "detail": response.text[:500]}


def _retryable(status: int, problem: dict[str, Any]) -> bool:
    """Whether the same request may succeed if repeated: what the problem says (its
    ``retryable`` flag, from the reason-code catalog), or by status when it says nothing."""
    flag = problem.get("retryable")
    return flag if isinstance(flag, bool) else status in RETRYABLE_STATUS


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After", "")
    if header.isdigit():
        return min(float(header), 60.0)
    return min(0.5 * 2.0 ** (attempt - 1), 10.0)


def _parse_sse(lines: Iterator[str]) -> Iterator[Event | str]:
    """Events, and bare ``id:`` positions (as strings) the server sends to mark where a stream
    started."""
    data: list[str] = []
    event_id: str | None = None
    for line in lines:
        if line == "":
            if data:
                payload: Event = json.loads("\n".join(data))
                yield payload
            elif event_id is not None:
                yield event_id
            data, event_id = [], None
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line.startswith("id:"):
            event_id = line[3:].strip()
