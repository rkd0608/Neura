"""Security regressions found in the M7 security review (design §17.7)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import psycopg
import pytest
from test_api_conventions import new_project

from conftest import ConformanceClient, contract
from neura_control.db.engine import create_engine, session_factory
from neura_control.db.models import Operation
from neura_control.services import site_reports

Api = tuple[ConformanceClient, str]


def test_a_retried_token_creation_replays_its_secret_but_the_database_never_holds_it(
    api: Api, database_url: str
) -> None:
    client, org = api
    body = {"display_name": "ci", "scopes": ["read"], "expires_in_days": 7}
    key = "f3a1c9e07b2d4c58a6e1d2f0b9c8a7e6"
    first = client.post(f"/v1/organizations/{org}/api-tokens", body, idempotency_key=key)
    again = client.post(f"/v1/organizations/{org}/api-tokens", body, idempotency_key=key)
    assert first.status_code == again.status_code == 201
    secret = first.json()["secret"]
    assert again.json()["secret"] == secret
    assert again.headers["Location"] == first.headers["Location"]
    with psycopg.connect(database_url) as conn:
        rows = conn.execute("SELECT key, response::text FROM idempotency_keys").fetchall()
    assert rows
    for stored_key, response in rows:
        assert stored_key != key  # only its hash
        assert secret not in response
        assert set(json.loads(response)) == {"sealed"}


def test_audit_records_cannot_be_changed_or_deleted(api: Api, database_url: str) -> None:
    client, org = api
    new_project(client, org, "audited")
    with psycopg.connect(database_url, autocommit=True) as conn:
        count = conn.execute("SELECT count(*) FROM audit_records").fetchone()
        assert count is not None
        assert count[0] > 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_records SET action = 'nothing'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM audit_records")


def _site_with_robot(client: ConformanceClient, project: str, name: str) -> str:
    profile = client.post(
        f"/v1/projects/{project}/robot-profiles", {"document": contract("so101.robot.yaml")}
    )
    profile_id = profile.json()["metadata"]["id"] if profile.status_code == 201 else None
    if profile_id is None:
        listed = client.get(f"/v1/projects/{project}/robot-profiles").json()["items"]
        profile_id = listed[0]["metadata"]["id"]
    site = client.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": name, "display_name": name, "offline_authorization_hours": 1}},
    ).json()["metadata"]["id"]
    robot: dict[str, Any] = client.post(
        f"/v1/projects/{project}/robots",
        {
            "spec": {
                "name": f"{name}-r",
                "display_name": "R",
                "site_id": site,
                "robot_profile_id": profile_id,
            }
        },
    ).json()
    return str(robot["metadata"]["id"])


def test_a_site_cannot_update_another_robots_session(api: Api, database_url: str) -> None:
    client, org = api
    project = new_project(client, org, "sessions")["metadata"]["id"]
    robot_a = _site_with_robot(client, project, "a")
    robot_b = _site_with_robot(client, project, "b")

    async def report(robot: str, state: str) -> None:
        engine = create_engine(database_url, pool_size=1)
        try:
            async with session_factory(engine).begin() as session:
                await site_reports.upsert_session(
                    session,
                    session_id="ses_01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    robot_id=robot,
                    revision_id="rev_x",
                    lease_generation=1,
                    task_epoch=1,
                    state=state,
                )
        finally:
            await engine.dispose()

    asyncio.run(report(robot_a, "Running"))
    with pytest.raises(LookupError):
        asyncio.run(report(robot_b, "Faulted"))
    with psycopg.connect(database_url) as conn:
        row = conn.execute("SELECT robot_id, state FROM robot_sessions").fetchone()
    assert row == (robot_a, "Running")


def _other_organization(database_url: str) -> tuple[str, str]:
    from neura_control.services import bootstrap

    async def create() -> tuple[str, str]:
        engine = create_engine(database_url, pool_size=1)
        try:
            async with session_factory(engine).begin() as session:
                other = await bootstrap.ensure_organization(session, "Other Robotics")
                token = await bootstrap.create_api_token(
                    session, other.id, display_name="other admin", scopes=["admin"]
                )
                return other.id, token
        finally:
            await engine.dispose()

    return asyncio.run(create())


def test_a_site_cannot_switch_robots_it_was_not_asked_to(api: Api, settings: Any) -> None:
    from test_onboarding_flow import onboard, prepared_deployment, site_ack

    client, org = api
    ours = onboard(client, org)
    our_deployment, our_revision = prepared_deployment(client, ours, settings)
    other_org, other_token = _other_organization(str(settings.database_url))
    stranger = client.as_token(other_token)
    theirs = onboard(stranger, other_org)
    their_deployment, their_revision = prepared_deployment(stranger, theirs, settings)
    activation = stranger.post(
        f"/v1/deployments/{their_deployment}/activation-requests", {"revision_id": their_revision}
    )
    assert activation.status_code == 202, activation.text

    # Their site acknowledges its own activation, naming our revision and our robot alongside
    # one of theirs: only what the activation asked for, of what the site reports, switches.
    site_ack(
        str(settings.database_url),
        activation.json()["metadata"]["id"],
        "Succeeded",
        result={"revision_id": our_revision, "robot_ids": [ours["robots"][0], theirs["robots"][0]]},
    )
    robot = client.get(f"/v1/robots/{ours['robots'][0]}").json()["status"]
    assert robot["active_revision_id"] is None
    status = client.get(f"/v1/deployments/{our_deployment}").json()["status"]
    assert set(status["active_revision_ids"].values()) == {None}
    status = stranger.get(f"/v1/deployments/{their_deployment}").json()["status"]
    assert status["active_revision_ids"] == {
        theirs["robots"][0]: their_revision,
        theirs["robots"][1]: None,
    }

    # And the writer itself refuses a revision or robots outside the operation's site.
    async def switch(revision_id: str, robot_ids: list[str]) -> None:
        engine = create_engine(str(settings.database_url), pool_size=1)
        try:
            async with session_factory(engine).begin() as session:
                operation = await session.get(Operation, activation.json()["metadata"]["id"])
                assert operation is not None
                await site_reports.set_active_revision(session, operation, revision_id, robot_ids)
        finally:
            await engine.dispose()

    asyncio.run(switch(our_revision, [ours["robots"][0]]))
    asyncio.run(switch(their_revision, [ours["robots"][1]]))
    for robot_id in ours["robots"][:2]:
        assert client.get(f"/v1/robots/{robot_id}").json()["status"]["active_revision_id"] is None
    status = client.get(f"/v1/deployments/{our_deployment}").json()["status"]
    assert set(status["active_revision_ids"].values()) == {None}


def test_oversized_bodies_are_refused_before_they_are_read(api: Api) -> None:
    client, org = api
    huge = {"spec": {"name": "x", "display_name": "x" * (5 * 1024 * 1024)}}
    response = client.post(f"/v1/organizations/{org}/projects", huge)
    assert response.status_code == 413
    assert response.json()["reason"] == "API_PAYLOAD_TOO_LARGE"


def test_garbage_credentials_from_one_address_are_rate_limited(make_api: Any) -> None:
    client, org = make_api(settings_overrides={"rate_limit_per_address_per_minute": 3})
    statuses = [
        client.as_token(f"garbage-{i}").get(f"/v1/organizations/{org}").status_code
        for i in range(5)
    ]
    assert statuses[:3] == [401, 401, 401]
    assert statuses[3:] == [429, 429]


def test_the_rate_limiter_forgets_idle_keys_first() -> None:
    from neura_control.app import RateLimiter

    limiter = RateLimiter(60, max_keys=2)
    assert limiter.take("busy", 0.0) is None
    for _ in range(59):
        limiter.take("busy", 0.0)
    assert limiter.take("busy", 0.0) is not None  # exhausted
    limiter.take("idle", 0.0)
    limiter.take("busy", 0.0)  # used most recently
    limiter.take("new", 0.0)  # evicts "idle", not "busy"
    assert limiter.take("busy", 0.0) is not None


def test_event_streams_are_capped(make_api: Any) -> None:
    client, org = make_api(settings_overrides={"events_max_streams": 0})
    project = new_project(client, org, "streams")["metadata"]["id"]
    response = client.get(f"/v1/projects/{project}/events")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"


def test_organizations_cannot_see_each_other(api: Api, database_url: str) -> None:
    client, org = api
    project = new_project(client, org, "ours")["metadata"]["id"]
    other_org, other_token = _other_organization(database_url)
    assert other_org != org
    stranger = client.as_token(other_token)
    for path in (
        f"/v1/organizations/{org}",
        f"/v1/organizations/{org}/projects",
        f"/v1/organizations/{org}/api-tokens",
        f"/v1/projects/{project}",
        f"/v1/projects/{project}/sites",
        f"/v1/projects/{project}/audit-records",
    ):
        assert stranger.get(path).status_code == 404, path
    # And the other way round.
    assert client.get(f"/v1/organizations/{other_org}").status_code == 404


def test_no_cross_origin_access_is_granted(api: Api) -> None:
    client, org = api
    response = client.get(
        f"/v1/organizations/{org}", headers={"Origin": "https://evil.example.com"}
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    preflight = client.client.options(
        f"/v1/organizations/{org}",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "DELETE",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers
