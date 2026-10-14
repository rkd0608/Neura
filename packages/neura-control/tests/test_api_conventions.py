"""API conventions (design §20): auth, roles, idempotency, If-Match, pagination, errors, limits."""

from __future__ import annotations

import concurrent.futures
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from conftest import VALIDATOR, AppFactory, ConformanceClient, contract
from neura_control.app import create_app, iter_routes
from neura_control.db.models import Base
from neura_control.security.oidc import OidcVerifier
from neura_control.security.principal import Role

Api = tuple[ConformanceClient, str]


def new_project(
    client: ConformanceClient, org: str, name: str = "p1", **labels: str
) -> dict[str, Any]:
    response = client.post(
        f"/v1/organizations/{org}/projects",
        {"spec": {"name": name, "display_name": name.upper(), "labels": labels}},
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


# --- the validator itself ---------------------------------------------------------------------


def test_spec_validator_rejects_nonconforming_bodies() -> None:
    with pytest.raises(AssertionError, match="does not match the spec"):
        VALIDATOR.validate("GET", "/v1/meta", 200, "application/json", b'{"service": 1}')
    with pytest.raises(AssertionError, match="not documented"):
        VALIDATOR.validate("GET", "/v1/meta", 418, "application/json", b"{}")
    with pytest.raises(AssertionError, match="no such path"):
        VALIDATOR.validate("GET", "/v1/nope", 200, "application/json", b"{}")


def test_every_spec_operation_is_implemented(api: Api) -> None:
    client, _ = api
    implemented = set(iter_routes(client.client.app))  # type: ignore[arg-type]
    assert len(implemented) > 50
    missing = [
        (method.upper(), path)
        for path, item in VALIDATOR.spec["paths"].items()
        for method in item
        if method in {"get", "post", "patch", "delete"}
        and (method.upper(), path) not in implemented
    ]
    assert missing == []


def test_migrations_match_models(database_url: str) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(database_url.replace("postgresql://", "postgresql+psycopg://", 1))
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()
    assert diff == [], "models changed without a migration; run alembic revision --autogenerate"


# --- authentication and roles ------------------------------------------------------------------


def test_requests_need_valid_credentials(api: Api) -> None:
    client, org = api
    for token in (None, "garbage", "ntk_tok_01ARZ3NDEKTSV4RRFFQ69G5FAV_" + "x" * 43):
        response = client.as_token(token).get(f"/v1/organizations/{org}")
        assert response.status_code == 401
        assert response.json()["reason"] == "API_UNAUTHENTICATED"


def test_me_reports_organizations_and_permissions(api: Api) -> None:
    client, org = api
    me = client.get("/v1/me").json()
    assert me["kind"] == "api_token"
    assert me["organizations"] == [
        {"id": org, "display_name": "Acme Robotics", "permissions": ["admin", "deploy", "read"]}
    ]
    assert me["project_ids"] is None


def test_invisible_resources_are_not_found(api: Api) -> None:
    client, _ = api
    response = client.get("/v1/organizations/org_01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_scoped_tokens(api: Api) -> None:
    client, org = api
    p1 = new_project(client, org, "p1")["metadata"]["id"]
    p2 = new_project(client, org, "p2")["metadata"]["id"]
    created = client.post(
        f"/v1/organizations/{org}/api-tokens",
        {
            "display_name": "ci reader",
            "scopes": ["read"],
            "project_ids": [p1],
            "expires_in_days": 7,
        },
    )
    assert created.status_code == 201
    secret = created.json()["secret"]
    reader = client.as_token(secret)
    assert reader.get(f"/v1/projects/{p1}").status_code == 200
    assert reader.get(f"/v1/projects/{p2}").status_code == 404  # outside the token's projects
    listed = reader.get(f"/v1/organizations/{org}/projects").json()["items"]
    assert [p["metadata"]["id"] for p in listed] == [p1]
    denied = reader.post(
        f"/v1/projects/{p1}/sites",
        {"spec": {"name": "s", "display_name": "S", "offline_authorization_hours": 1}},
    )
    assert denied.status_code == 403
    assert denied.json()["reason"] == "API_FORBIDDEN"
    # Revocation takes effect immediately.
    token_id = created.json()["metadata"]["id"]
    assert client.delete(f"/v1/api-tokens/{token_id}", version="1").status_code == 204
    assert reader.get(f"/v1/projects/{p1}").status_code == 401
    assert all(
        t["metadata"]["id"] != token_id
        for t in client.get(f"/v1/organizations/{org}/api-tokens").json()["items"]
    )


def test_expired_token_is_rejected(api: Api, database_url: str) -> None:
    client, org = api
    created = client.post(
        f"/v1/organizations/{org}/api-tokens",
        {"display_name": "short", "scopes": ["read"], "expires_in_days": 1},
    )
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE api_tokens SET expires_at = now() - interval '1 second' WHERE id = %s",
            (created.json()["metadata"]["id"],),
        )
    assert (
        client.as_token(created.json()["secret"]).get(f"/v1/organizations/{org}").status_code == 401
    )


class _Issuer:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.verifier = OidcVerifier(
            issuer="https://sso.example.com",
            audience="neura",
            key_resolver=lambda _: self.key.public_key(),
        )

    def token(self, sub: str, *, audience: str = "neura", expires_in: int = 300) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "iss": "https://sso.example.com",
                "sub": sub,
                "aud": audience,
                "iat": now,
                "exp": now + timedelta(seconds=expires_in),
            },
            self.key,
            algorithm="RS256",
        )


def test_oidc_users_get_their_role(make_api: AppFactory) -> None:
    issuer = _Issuer()
    client, org = make_api(
        oidc_verifier=issuer.verifier,
        memberships={
            "https://sso.example.com|alice": Role.VIEWER,
            "https://sso.example.com|bob": Role.DEPLOYER,
        },
    )
    project = new_project(client, org)["metadata"]["id"]
    alice = client.as_token(issuer.token("alice"))
    assert alice.get(f"/v1/projects/{project}").status_code == 200
    assert (
        alice.post(
            f"/v1/projects/{project}/robot-profiles", {"document": contract("so101.robot.yaml")}
        ).status_code
        == 403
    )
    bob = client.as_token(issuer.token("bob"))
    assert (
        bob.post(
            f"/v1/projects/{project}/robot-profiles", {"document": contract("so101.robot.yaml")}
        ).status_code
        == 201
    )
    # Deployers cannot administer.
    assert (
        bob.post(
            f"/v1/organizations/{org}/projects", {"spec": {"name": "x", "display_name": "X"}}
        ).status_code
        == 403
    )
    # Strangers, wrong audiences and expired tokens are refused.
    assert (
        client.as_token(issuer.token("mallory")).get(f"/v1/projects/{project}").status_code == 404
    )
    assert (
        client.as_token(issuer.token("alice", audience="other"))
        .get(f"/v1/projects/{project}")
        .status_code
        == 401
    )
    assert (
        client.as_token(issuer.token("alice", expires_in=-120))
        .get(f"/v1/projects/{project}")
        .status_code
        == 401
    )


# --- idempotency -------------------------------------------------------------------------------


def test_idempotent_create(api: Api) -> None:
    client, org = api
    body = {"spec": {"name": "once", "display_name": "Once"}}
    first = client.post(
        f"/v1/organizations/{org}/projects", body, idempotency_key="key-0123456789abcdef"
    )
    second = client.post(
        f"/v1/organizations/{org}/projects", body, idempotency_key="key-0123456789abcdef"
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["Location"] == first.headers["Location"]
    projects = client.get(f"/v1/organizations/{org}/projects").json()["items"]
    assert len(projects) == 1
    reused = client.post(
        f"/v1/organizations/{org}/projects",
        {"spec": {"name": "other", "display_name": "Other"}},
        idempotency_key="key-0123456789abcdef",
    )
    assert reused.status_code == 422
    assert reused.json()["reason"] == "API_IDEMPOTENCY_KEY_REUSED"
    missing = client.post(f"/v1/organizations/{org}/projects", body, idempotency_key=None)
    assert missing.status_code == 428
    assert missing.json()["reason"] == "API_IDEMPOTENCY_KEY_REQUIRED"


def test_concurrent_retries_create_one_resource(api: Api) -> None:
    client, org = api
    body = {"spec": {"name": "race", "display_name": "Race"}}

    def call(_: int) -> int:
        return client.post(
            f"/v1/organizations/{org}/projects", body, idempotency_key="race-0123456789abcd"
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(call, range(6)))
    assert set(statuses) == {201}
    assert len(client.get(f"/v1/organizations/{org}/projects").json()["items"]) == 1


def test_failed_requests_are_not_remembered(api: Api) -> None:
    client, org = api
    key = "fail-0123456789abcdef"
    project = new_project(client, org)["metadata"]["id"]
    body = {"spec": {"name": "s1", "display_name": "S", "offline_authorization_hours": 1}}
    client.post(f"/v1/projects/{project}/sites", body)
    duplicate = client.post(f"/v1/projects/{project}/sites", body, idempotency_key=key)
    assert duplicate.status_code == 409  # name taken; not stored under the key
    # The failure was not stored under the key, so a corrected retry with it simply succeeds.
    renamed = {"spec": {**body["spec"], "name": "s2"}}
    assert (
        client.post(f"/v1/projects/{project}/sites", renamed, idempotency_key=key).status_code
        == 201
    )


# --- optimistic concurrency --------------------------------------------------------------------


def test_if_match_is_required_and_enforced(api: Api) -> None:
    client, org = api
    project = new_project(client, org, region="eu")
    path = f"/v1/projects/{project['metadata']['id']}"
    assert client.patch(path, {"spec": {"display_name": "X"}}, version=None).status_code == 428
    stale = client.patch(path, {"spec": {"display_name": "X"}}, version="7")
    assert stale.status_code == 412
    updated = client.patch(
        path,
        {"spec": {"display_name": "Renamed", "labels": {"region": None, "tier": "gold"}}},
        version="1",
    )
    assert updated.status_code == 200
    assert updated.headers["ETag"] == '"2"'
    assert updated.json()["spec"]["labels"] == {"tier": "gold"}  # merge patch: null deletes
    again = client.patch(path, {"spec": {"display_name": "Again"}}, version="1")
    assert again.status_code == 412
    immutable = client.patch(path, {"spec": {"name": "new-name"}}, version="2")
    assert immutable.status_code == 422
    wrong_type = client.patch(
        path, {"spec": {"display_name": "Y"}}, version="2", content_type="application/json"
    )
    assert wrong_type.status_code == 422


# --- validation and conflicts ------------------------------------------------------------------


def test_contract_validation(api: Api) -> None:
    client, org = api
    project = new_project(client, org)["metadata"]["id"]
    bad = contract("so101.robot.yaml")
    bad["spec"]["control_period_us"] = 0
    response = client.post(f"/v1/projects/{project}/robot-profiles", {"document": bad})
    assert response.status_code == 422
    assert any("control_period_us" in e for e in response.json()["errors"])
    wrong_kind = client.post(
        f"/v1/projects/{project}/model-artifacts", {"document": contract("so101.robot.yaml")}
    )
    assert wrong_kind.status_code == 422
    unbound = client.post(
        f"/v1/projects/{project}/execution-profiles",
        {"document": contract("act-so101-30hz.profile.yaml")},
    )
    assert unbound.status_code == 422  # its artifact and robot profile are not registered yet
    assert "not registered" in unbound.json()["detail"]


def test_incompatible_revision_is_refused_with_reasons(api: Api) -> None:
    client, org = api
    project = new_project(client, org)["metadata"]["id"]
    robot = contract("so101.robot.yaml")
    rp = client.post(f"/v1/projects/{project}/robot-profiles", {"document": robot}).json()[
        "metadata"
    ]["id"]
    art = client.post(
        f"/v1/projects/{project}/model-artifacts", {"document": contract("act-so101.artifact.yaml")}
    ).json()["metadata"]["id"]
    exp = client.post(
        f"/v1/projects/{project}/execution-profiles",
        {"document": contract("act-so101-30hz.profile.yaml")},
    ).json()["metadata"]["id"]
    # A second robot profile at 50 Hz makes the 30 Hz execution profile incompatible.
    robot50 = contract("so101.robot.yaml")
    robot50["metadata"]["version"] = "r2"
    robot50["spec"]["control_period_us"] = 20000
    rp50 = client.post(f"/v1/projects/{project}/robot-profiles", {"document": robot50}).json()[
        "metadata"
    ]["id"]
    site = client.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "s", "display_name": "S", "offline_authorization_hours": 1}},
    ).json()["metadata"]["id"]
    rb = client.post(
        f"/v1/projects/{project}/robots",
        {"spec": {"name": "r", "display_name": "R", "site_id": site, "robot_profile_id": rp50}},
    ).json()["metadata"]["id"]
    dep = client.post(
        f"/v1/projects/{project}/deployments",
        {
            "spec": {
                "name": "d",
                "display_name": "D",
                "site_id": site,
                "robot_ids": [rb],
                "rollout": {"strategy": "all_at_task_boundary"},
            }
        },
    ).json()["metadata"]["id"]
    response = client.post(
        f"/v1/deployments/{dep}/revisions",
        {"spec": {"model_artifact_id": art, "execution_profile_id": exp, "robot_profile_id": rp50}},
    )
    assert response.status_code == 422
    assert any("COMPAT_ROBOT_PROFILE_DIGEST_MISMATCH" in e for e in response.json()["errors"])
    mismatch = client.post(
        f"/v1/deployments/{dep}/revisions",
        {"spec": {"model_artifact_id": art, "execution_profile_id": exp, "robot_profile_id": rp}},
    )
    assert mismatch.status_code == 422  # the deployment's robot uses another profile
    check = client.post(
        f"/v1/projects/{project}/compatibility-checks",
        {"model_artifact_id": art, "robot_profile_id": rp50, "execution_profile_id": exp},
        idempotency_key=None,
    )
    assert check.json()["compatible"] is False
    assert "COMPAT_CONTROL_PERIOD_MISMATCH" in {i["code"] for i in check.json()["issues"]}


def test_referential_rules(api: Api) -> None:
    client, org = api
    project = new_project(client, org)["metadata"]["id"]
    rp = client.post(
        f"/v1/projects/{project}/robot-profiles", {"document": contract("so101.robot.yaml")}
    ).json()["metadata"]["id"]
    s1 = client.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "a", "display_name": "A", "offline_authorization_hours": 1}},
    ).json()
    s2 = client.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "b", "display_name": "B", "offline_authorization_hours": 1}},
    ).json()
    robot = client.post(
        f"/v1/projects/{project}/robots",
        {
            "spec": {
                "name": "r",
                "display_name": "R",
                "site_id": s1["metadata"]["id"],
                "robot_profile_id": rp,
            }
        },
    ).json()
    dup = client.post(
        f"/v1/projects/{project}/robots",
        {
            "spec": {
                "name": "r",
                "display_name": "R2",
                "site_id": s1["metadata"]["id"],
                "robot_profile_id": rp,
            }
        },
    )
    assert dup.status_code == 409
    wrong_site = client.post(
        f"/v1/projects/{project}/deployments",
        {
            "spec": {
                "name": "d",
                "display_name": "D",
                "site_id": s2["metadata"]["id"],
                "robot_ids": [robot["metadata"]["id"]],
                "rollout": {"strategy": "all_at_task_boundary"},
            }
        },
    )
    assert wrong_site.status_code == 422
    dep = client.post(
        f"/v1/projects/{project}/deployments",
        {
            "spec": {
                "name": "d",
                "display_name": "D",
                "site_id": s1["metadata"]["id"],
                "robot_ids": [robot["metadata"]["id"]],
                "rollout": {"strategy": "all_at_task_boundary"},
            }
        },
    )
    assert dep.status_code == 201
    overlap = client.post(
        f"/v1/projects/{project}/deployments",
        {
            "spec": {
                "name": "d2",
                "display_name": "D2",
                "site_id": s1["metadata"]["id"],
                "robot_ids": [robot["metadata"]["id"]],
                "rollout": {"strategy": "all_at_task_boundary"},
            }
        },
    )
    assert overlap.status_code == 409
    assert client.delete(f"/v1/sites/{s1['metadata']['id']}", version="1").status_code == 409
    assert client.delete(f"/v1/robots/{robot['metadata']['id']}", version="1").status_code == 409
    assert (
        client.delete(f"/v1/deployments/{dep.json()['metadata']['id']}", version="1").status_code
        == 204
    )
    assert client.delete(f"/v1/robots/{robot['metadata']['id']}", version="1").status_code == 204
    assert client.delete(f"/v1/sites/{s1['metadata']['id']}", version="1").status_code == 204
    assert client.get(f"/v1/sites/{s1['metadata']['id']}").status_code == 404


# --- lists -------------------------------------------------------------------------------------


def test_cursor_pagination_and_label_filters(api: Api) -> None:
    client, org = api
    for i in range(7):
        new_project(client, org, f"p{i}", tier="gold" if i % 2 else "silver")
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"page_size": 3}
        if cursor:
            params["cursor"] = cursor
        page = client.get(f"/v1/organizations/{org}/projects", params=params).json()
        seen += [p["spec"]["name"] for p in page["items"]]
        cursor = page["page"]["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"p{i}" for i in range(7)]
    gold = client.get(f"/v1/organizations/{org}/projects", params={"label": "tier=gold"}).json()[
        "items"
    ]
    assert [p["spec"]["name"] for p in gold] == ["p1", "p3", "p5"]
    assert (
        client.get(f"/v1/organizations/{org}/projects", params={"page_size": 0}).status_code == 422
    )
    assert (
        client.get(f"/v1/organizations/{org}/projects", params={"label": "nolabel"}).status_code
        == 422
    )


# --- events, metrics, grants -------------------------------------------------------------------


def test_event_stream_resumes_from_cursor(api: Api, database_url: str) -> None:
    client, org = api
    project = new_project(client, org)["metadata"]["id"]
    client.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "s", "display_name": "S", "offline_authorization_hours": 1}},
    )
    headers = {"Authorization": f"Bearer {client.token}"}
    with client.client.stream(
        "GET", f"/v1/projects/{project}/events", params={"cursor": "0"}, headers=headers
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(line)
                break
    assert '"type":"resource.created"' in events[0]
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DELETE FROM events WHERE cursor = (SELECT min(cursor) FROM events)")
    expired = client.get(f"/v1/projects/{project}/events", params={"cursor": "0"}, validate=False)
    assert expired.status_code == 410
    assert expired.json()["reason"] == "API_CURSOR_EXPIRED"


def test_metrics_query_returns_aggregates(api: Api, database_url: str) -> None:
    client, org = api
    project = new_project(client, org)["metadata"]["id"]
    site = client.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "s", "display_name": "S", "offline_authorization_hours": 1}},
    ).json()["metadata"]["id"]
    start = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    with psycopg.connect(database_url, autocommit=True) as conn:
        for minute, value in ((0, 1.0), (0, 3.0), (2, 5.0)):
            conn.execute(
                "INSERT INTO metric_points (project_id, site_id, metric, ts, value) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    project,
                    site,
                    "deadline_miss_rate",
                    start + timedelta(minutes=minute, seconds=5),
                    value,
                ),
            )
    response = client.post(
        f"/v1/projects/{project}/metrics/query",
        {
            "metric": "deadline_miss_rate",
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=3)).isoformat(),
            "step_seconds": 60,
        },
        idempotency_key=None,
    )
    assert response.status_code == 200, response.text
    points = response.json()["series"][0]["points"]
    assert [p[1] for p in points] == [2.0, None, 5.0]


def test_support_grants(api: Api) -> None:
    client, org = api
    project = new_project(client, org)["metadata"]["id"]
    site = client.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "s", "display_name": "S", "offline_authorization_hours": 1}},
    ).json()["metadata"]["id"]
    grant = client.post(
        f"/v1/organizations/{org}/support-grants",
        {
            "site_id": site,
            "operations": ["read_status", "collect_diagnostics"],
            "duration_minutes": 60,
            "justification": "investigating a starvation report",
        },
    )
    assert grant.status_code == 201
    assert grant.json()["state"] == "Active"
    grant_id = grant.json()["metadata"]["id"]
    assert client.delete(f"/v1/support-grants/{grant_id}", version="1").status_code == 204
    states = [
        g["state"] for g in client.get(f"/v1/organizations/{org}/support-grants").json()["items"]
    ]
    assert states == ["Revoked"]


def test_rate_limit(make_api: AppFactory) -> None:
    client, org = make_api(settings_overrides={"rate_limit_per_minute": 3})
    statuses = [client.get(f"/v1/organizations/{org}").status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    limited = client.get(f"/v1/organizations/{org}")
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.json()["reason"] == "API_RATE_LIMITED"


def test_request_ids_are_returned(api: Api) -> None:
    client, org = api
    response = client.get(f"/v1/organizations/{org}", headers={"X-Request-Id": "trace-abc"})
    assert response.headers["X-Request-Id"] == "trace-abc"
    assert len(client.get(f"/v1/organizations/{org}").headers["X-Request-Id"]) == 24


def test_dev_bootstrap_token(settings: Any) -> None:
    from neura_control.services.bootstrap import generate_token_value

    token = generate_token_value()
    dev = settings.model_copy(update={"environment": "development", "dev_bootstrap_token": token})
    with TestClient(create_app(dev)) as client:
        orgs = client.get("/v1/meta")
        assert orgs.status_code == 200
        started = time.monotonic()
        response = client.get(
            "/v1/organizations/org_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 404  # authenticated, the organization just is not this one
        assert time.monotonic() - started < 5
