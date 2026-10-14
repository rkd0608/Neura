from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from neura_agent import AgentSettings, create_app
from neura_agent.connectivity import ControlPlaneProbe
from neura_contracts import ReasonCode


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _handler(status: int) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, json={"status": "ok"}))


def _failing() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


async def test_probe_reports_unchecked_before_first_check() -> None:
    async with httpx.AsyncClient(transport=_handler(200)) as client:
        probe = ControlPlaneProbe(client, "http://cp", interval_s=5)
        snapshot = probe.snapshot()
    assert snapshot.checked is False
    assert snapshot.reachable is False
    assert snapshot.reason is None


async def test_probe_success_and_ages_use_local_monotonic_clock() -> None:
    clock = FakeClock()
    async with httpx.AsyncClient(transport=_handler(200)) as client:
        probe = ControlPlaneProbe(client, "http://cp/", interval_s=5, clock=clock)
        assert await probe.check_once()
        clock.now += 7.5
        snapshot = probe.snapshot()
    assert snapshot.reachable
    assert snapshot.last_checked_age_s == 7.5
    assert snapshot.last_success_age_s == 7.5
    assert snapshot.reason is None


async def test_probe_failure_reports_reason_and_keeps_last_success() -> None:
    clock = FakeClock()
    async with httpx.AsyncClient(transport=_handler(200)) as ok_client:
        probe = ControlPlaneProbe(ok_client, "http://cp", interval_s=5, clock=clock)
        await probe.check_once()
    clock.now += 3
    async with httpx.AsyncClient(transport=_failing()) as bad_client:
        probe._client = bad_client
        assert not await probe.check_once()
        assert not await probe.check_once()
    clock.now += 1
    snapshot = probe.snapshot()
    assert not snapshot.reachable
    assert snapshot.reason is ReasonCode.AGENT_CONTROL_PLANE_UNREACHABLE
    assert snapshot.consecutive_failures == 2
    assert snapshot.last_success_age_s == 4
    assert snapshot.detail is not None
    assert "ConnectError" in snapshot.detail


async def test_probe_treats_server_errors_as_unreachable() -> None:
    async with httpx.AsyncClient(transport=_handler(503)) as client:
        probe = ControlPlaneProbe(client, "http://cp", interval_s=5)
        assert not await probe.check_once()
        assert probe.snapshot().detail == "health check returned HTTP 503"


async def test_probe_start_stop_is_idempotent() -> None:
    async with httpx.AsyncClient(transport=_handler(200)) as client:
        probe = ControlPlaneProbe(client, "http://cp", interval_s=0.01)
        probe.start()
        probe.start()
        await probe.stop()
        await probe.stop()


def test_local_status_endpoint() -> None:
    app = create_app(AgentSettings(), transport=_failing(), start_probe=False)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        status = client.get("/v1/local/status").json()
    assert status["service"] == "neura-agent"
    assert status["enrolled"] is False
    assert status["protocol_version"] == "PROTOCOL_VERSION_1"
    assert status["control_plane"]["checked"] is False


def test_local_status_after_failed_probe() -> None:
    app = create_app(AgentSettings(), transport=_failing(), start_probe=False)
    with TestClient(app) as client:
        client.portal.call(app.state.probe.check_once)  # type: ignore[union-attr]
        status = client.get("/v1/local/status").json()
    assert status["control_plane"]["reason"] == "AGENT_CONTROL_PLANE_UNREACHABLE"
