"""Outbound reachability of the control plane.

The site never depends on this for operation (design §8, §17.3). It only reports whether the
management connection is up, using the agent's own monotonic clock for ages.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from neura_contracts import ReasonCode

Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class ConnectivitySnapshot:
    reachable: bool
    checked: bool
    last_checked_age_s: float | None
    last_success_age_s: float | None
    consecutive_failures: int
    reason: ReasonCode | None
    detail: str | None


class ControlPlaneProbe:
    """Periodically checks ``GET {control_plane_url}/healthz`` over an outbound connection."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        interval_s: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self._client = client
        self._url = base_url.rstrip("/") + "/healthz"
        self._interval_s = interval_s
        self._clock = clock
        self._last_checked: float | None = None
        self._last_success: float | None = None
        self._failures = 0
        self._detail: str | None = None
        self._task: asyncio.Task[None] | None = None

    async def check_once(self) -> bool:
        try:
            response = await self._client.get(self._url)
            ok = response.status_code == 200
            detail = None if ok else f"health check returned HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            ok = False
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        now = self._clock()
        self._last_checked = now
        if ok:
            self._last_success = now
            self._failures = 0
            self._detail = None
        else:
            self._failures += 1
            self._detail = detail
        return ok

    def snapshot(self) -> ConnectivitySnapshot:
        now = self._clock()
        checked = self._last_checked is not None
        reachable = checked and self._failures == 0
        return ConnectivitySnapshot(
            reachable=reachable,
            checked=checked,
            last_checked_age_s=None if self._last_checked is None else now - self._last_checked,
            last_success_age_s=None if self._last_success is None else now - self._last_success,
            consecutive_failures=self._failures,
            reason=None if reachable or not checked else ReasonCode.AGENT_CONTROL_PLANE_UNREACHABLE,
            detail=self._detail,
        )

    async def _run(self) -> None:
        while True:
            await self.check_once()
            await asyncio.sleep(self._interval_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="control-plane-probe")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
