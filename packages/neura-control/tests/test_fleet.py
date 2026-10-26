"""The workspace, policies, rollouts, activity and CLI sign-in (design §29).

The site is simulated by acknowledging its operations directly (``site_ack``), as the agent's
stream does; the rollout driver then advances on those reports. Every response is checked against
api/openapi.yaml by the conformance client.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from test_onboarding_flow import site_ack

from conftest import AppFactory, ConformanceClient, OidcIssuer, contract
from neura_contracts import signing
from neura_contracts.inference import lerobot_artifact
from neura_control.security.principal import Role

ADMIN = "https://sso.example.com|ada"


def act_config(chunk: int = 100) -> dict[str, Any]:
    return {
        "type": "act",
        "chunk_size": chunk,
        "n_action_steps": chunk,
        "input_features": {
            "observation.state": {"type": "STATE", "shape": [6]},
            "observation.images.front": {"type": "VISUAL", "shape": [3, 480, 640]},
            "observation.images.wrist": {"type": "VISUAL", "shape": [3, 480, 640]},
        },
        "output_features": {"action": {"type": "ACTION", "shape": [6]}},
    }


def artifact(version: str, weight: str = "a") -> dict[str, Any]:
    return lerobot_artifact(
        act_config(),
        contract("so101.robot.yaml"),
        name="pick",
        version=version,
        weights=[
            {"path": "model.safetensors", "digest": "sha256:" + weight * 64, "size_bytes": 1 << 20}
        ],
    )


class Fleet:
    """A workspace with a site of robots, as the API and a site's reports make it."""

    def __init__(self, client: ConformanceClient, org: str, database_url: str, kind: str):
        self.c, self.org, self.db = client, org, database_url
        workspace = client.get("/v1/workspace")
        assert workspace.status_code == 200, workspace.text
        self.project = workspace.json()["project_id"]
        site = client.post(
            f"/v1/projects/{self.project}/sites",
            {
                "spec": {
                    "name": "plant-1",
                    "display_name": "Plant 1",
                    "offline_authorization_hours": 72,
                    "kind": kind,
                }
            },
        )
        assert site.status_code == 201, site.text
        assert site.json()["spec"]["kind"] == kind
        self.site = site.json()["metadata"]["id"]
        profile = client.post(
            f"/v1/projects/{self.project}/robot-profiles",
            {"document": contract("so101.robot.yaml")},
        )
        assert profile.status_code == 201, profile.text
        self.robots = []
        for name in ("arm-1", "arm-2", "arm-3"):
            robot = client.post(
                f"/v1/projects/{self.project}/robots",
                {
                    "spec": {
                        "name": name,
                        "display_name": name,
                        "site_id": self.site,
                        "robot_profile_id": profile.json()["metadata"]["id"],
                    }
                },
            )
            assert robot.status_code == 201, robot.text
            self.robots.append(robot.json()["metadata"]["id"])
        policy = client.post(f"/v1/projects/{self.project}/policies", {"spec": {"name": "pick"}})
        assert policy.status_code == 201, policy.text
        self.policy = policy.json()["metadata"]["id"]
        self.key = signing.load_private_key(signing.generate_key())

    def push(self, version: str, weight: str) -> dict[str, Any]:
        document = artifact(version, weight)
        from neura_contracts import parse_document

        digest = parse_document(document).digest()
        response = self.c.post(
            f"/v1/policies/{self.policy}/versions",
            {
                "spec": {
                    "source": {"kind": "lerobot", "reference": f"./checkpoints/{version}"},
                    "model_artifact": {
                        "document": document,
                        "signatures": [signing.sign(digest, self.key)],
                    },
                }
            },
        )
        assert response.status_code in (200, 201), response.text
        data: dict[str, Any] = response.json()
        return data

    def deploy(self, version: str, robots: list[str], **spec: Any) -> dict[str, Any]:
        response = self.c.post(
            f"/v1/projects/{self.project}/rollouts",
            {"spec": {"robot_ids": robots, "policy_version_id": version, **spec}},
        )
        assert response.status_code == 201, response.text
        data: dict[str, Any] = response.json()
        return data

    def rollout(self, rollout_id: str) -> dict[str, Any]:
        response = self.c.get(f"/v1/rollouts/{rollout_id}")
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()
        return data

    def operation_of(self, rollout_id: str) -> str:
        # The operation a rollout waits on is the newest one targeting its revision or job.
        rollout = self.rollout(rollout_id)
        operations = self.c.get(
            f"/v1/projects/{self.project}/audit-records", params={"page_size": 200}
        )
        assert operations.status_code == 200
        targets = [
            r["target"]
            for r in operations.json()["items"]
            if r["action"].startswith("operation.") and r["actor"] == "neura:rollouts"
        ]
        assert targets, rollout
        return str(targets[-1])

    def ack(self, rollout_id: str, state: str, **kwargs: Any) -> dict[str, Any]:
        site_ack(self.db, self.operation_of(rollout_id), state, **kwargs)
        return self.rollout(rollout_id)

    def switched(self, rollout_id: str, revision: str, robots: list[str]) -> dict[str, Any]:
        """The site's operator approved and every robot switched."""
        result = {"approved_by": "op", "revision_id": revision, "robot_ids": robots}
        return self.ack(rollout_id, "Succeeded", result=result)


def measured(p99_ms: float) -> dict[str, Any]:
    return {
        "revision_digest": "sha256:" + "0" * 64,
        "capacity": {
            "max_robots": 8,
            "limiting_factor": "deadline",
            "source": "measured_profile",
            "assumptions": {
                "control_period_ms": 33.3,
                "starvation_budget_ms": 600,
                "request_interval_ms": 1000,
                "link_allowance_ms": 20,
                "max_batch": 8,
                "reserve_fraction": 0.2,
                "latency_p99_ms_by_batch": {"1": p99_ms, "2": p99_ms * 1.4, "4": p99_ms * 2},
                "device": "cuda:0",
            },
        },
    }


def steps(rollout: dict[str, Any]) -> dict[str, str]:
    return {s["name"]: s["state"] for s in rollout["status"]["steps"]}


@pytest.fixture
def bench(api: tuple[ConformanceClient, str], settings: Any) -> Fleet:
    client, org = api
    return Fleet(client, org, str(settings.database_url), "bench")


@pytest.fixture
def production(api: tuple[ConformanceClient, str], settings: Any) -> Fleet:
    client, org = api
    return Fleet(client, org, str(settings.database_url), "production")


def test_workspace_is_the_callers_organization_and_its_project(
    api: tuple[ConformanceClient, str],
) -> None:
    client, org = api
    first = client.get("/v1/workspace").json()
    assert first["organization_id"] == org
    assert first["permissions"] == ["admin", "deploy", "read"]
    # The default project is made once and kept.
    assert client.get("/v1/workspace").json()["project_id"] == first["project_id"]
    assert (
        client.get("/v1/workspace", params={"organization_id": "org_" + "0" * 26}).status_code
        == 404
    )


def test_versions_number_themselves_and_a_repush_is_the_same_version(bench: Fleet) -> None:
    v1 = bench.push("v1", "a")
    assert v1["status"]["number"] == 1
    assert v1["status"]["name"] == "pick v1"
    assert v1["status"]["adapter"] == "lerobot-act"
    assert v1["status"]["signed"] is True
    v2 = bench.push("v2", "b")
    assert v2["status"]["number"] == 2
    again = bench.push("v1", "a")
    assert again["metadata"]["id"] == v1["metadata"]["id"]
    listed = bench.c.get(f"/v1/policies/{bench.policy}/versions").json()["items"]
    assert [v["status"]["number"] for v in listed] == [2, 1]  # newest first
    policy = bench.c.get(f"/v1/policies/{bench.policy}").json()
    assert policy["status"] == {"versions": 2, "latest_version": 2}


def test_a_repush_signed_by_another_key_adds_its_signature_audited(bench: Fleet) -> None:
    first = bench.push("v1", "a")
    artifact_id = first["spec"]["model_artifact_id"]
    before = bench.c.get(f"/v1/model-artifacts/{artifact_id}").json()
    bench.key = signing.load_private_key(signing.generate_key())
    again = bench.push("v1", "a")
    assert again["metadata"]["id"] == first["metadata"]["id"]  # still the same version
    after = bench.c.get(f"/v1/model-artifacts/{artifact_id}").json()
    assert len(after["signatures"]) == 2
    assert after["metadata"]["version"] == "2"
    assert datetime.fromisoformat(after["metadata"]["updated_at"]) > datetime.fromisoformat(
        before["metadata"]["updated_at"]
    )
    audit = bench.c.get(
        f"/v1/projects/{bench.project}/audit-records", params={"page_size": 200}
    ).json()["items"]
    assert [r["target"] for r in audit if r["action"] == "model_artifact.sign"] == [artifact_id]
    with psycopg.connect(bench.db) as conn:
        events = conn.execute(
            "SELECT type, resource_version FROM events WHERE resource = %s ORDER BY cursor",
            (artifact_id,),
        ).fetchall()
    assert events == [("resource.created", "1"), ("resource.updated", "2")]


def test_a_versions_source_must_match_its_adapter(bench: Fleet) -> None:
    response = bench.c.post(
        f"/v1/policies/{bench.policy}/versions",
        {
            "spec": {
                "source": {"kind": "container"},
                "model_artifact": {"document": artifact("v1")},
            }
        },
    )
    assert response.status_code == 422
    assert "container version cannot use the lerobot-act adapter" in response.text


def test_deploy_on_a_bench_from_check_to_live(bench: Fleet) -> None:
    version = bench.push("v1", "a")["metadata"]["id"]
    robots = bench.robots[:2]
    rollout = bench.deploy(version, robots)
    rid = rollout["metadata"]["id"]
    status = rollout["status"]
    assert status["state"] == "preparing"
    assert status["name"] == "pick v1"
    assert steps(rollout) == {
        "check": "passed",
        "prepare": "running",
        "shadow": "pending",
        "approve": "pending",
        "switch": "pending",
    }
    assert "fits 2 so101-follower robots: 30 Hz, 100-action chunks" in status["steps"][0]["summary"]

    # The site measures 40 ms a pass (56 ms for the two robots batched): the limits change, so
    # the measured revision is prepared.
    rollout = bench.ack(rid, "Succeeded", result=measured(40))
    assert rollout["status"]["state"] == "preparing"
    first_revision = rollout["status"]["revision_id"]
    rollout = bench.ack(rid, "Succeeded", result=measured(40))
    assert rollout["status"]["revision_id"] == first_revision
    assert rollout["status"]["state"] == "awaiting_approval"
    assert steps(rollout)["prepare"] == "passed"
    assert "56 ms per pass (measured)" in rollout["status"]["steps"][1]["summary"]
    assert steps(rollout)["shadow"] == "skipped"  # nothing runs there yet
    assert "neura approve" in rollout["status"]["steps"][3]["summary"]

    rollout = bench.ack(
        rid, "Running", message="approved by ops", result={"approved_by": "ops@plant"}
    )
    assert rollout["status"]["state"] == "switching"
    assert rollout["status"]["approved_by"] == "ops@plant"
    rollout = bench.ack(
        rid,
        "Succeeded",
        result={"approved_by": "ops@plant", "revision_id": first_revision, "robot_ids": robots},
    )
    assert rollout["status"]["state"] == "live"
    assert all(s in ("passed", "skipped") for s in steps(rollout).values())

    robot = bench.c.get(f"/v1/robots/{robots[0]}").json()["status"]
    assert (robot["policy_name"], robot["policy_version"]) == ("pick", 1)
    running = bench.c.get(f"/v1/policy-versions/{version}").json()["status"]["running_robot_ids"]
    assert running == sorted(robots)

    activity = bench.c.get(
        f"/v1/projects/{bench.project}/activity", params={"rollout_id": rid}
    ).json()["items"]
    summaries = [a["summary"] for a in activity]
    assert summaries[0] == "arm-1, arm-2 now run pick v1"
    assert any("waiting for approval at plant-1" in s for s in summaries)
    assert summaries[-1].endswith("to arm-1, arm-2 at plant-1")


def test_a_second_version_is_shadowed_then_rolled_back(production: Fleet) -> None:
    fleet = production
    v1 = fleet.push("v1", "a")["metadata"]["id"]
    robots = fleet.robots[:1]
    rid = fleet.deploy(v1, robots)["metadata"]["id"]
    fleet.ack(rid, "Succeeded", result=measured(40))
    revision = fleet.ack(rid, "Succeeded", result=measured(40))["status"]["revision_id"]
    fleet.switched(rid, revision, robots)
    assert fleet.rollout(rid)["status"]["state"] == "live"

    v2 = fleet.push("v2", "b")["metadata"]["id"]
    second = fleet.deploy(v2, robots, shadow_s=60)
    sid = second["metadata"]["id"]
    fleet.ack(sid, "Succeeded", result=measured(40))
    rollout = fleet.ack(sid, "Succeeded", result=measured(40))
    assert rollout["status"]["state"] == "shadow_testing"
    assert "for 1 min" in rollout["status"]["steps"][2]["summary"]
    evaluation = fleet.c.get(f"/v1/evaluations/{rollout['status']['evaluation_id']}").json()
    assert evaluation["spec"]["baseline_revision_id"] == revision
    rollout = fleet.ack(
        sid,
        "Succeeded",
        result={"evaluation": {"kind": "shadow", "verdict": "non_inferior", "samples": 412}},
    )
    assert rollout["status"]["state"] == "awaiting_approval"
    assert "close to the running version over 412 observations" in str(rollout)
    new_revision = rollout["status"]["revision_id"]
    fleet.ack(
        sid,
        "Succeeded",
        result={"approved_by": "op", "revision_id": new_revision, "robot_ids": robots},
    )
    assert fleet.c.get(f"/v1/robots/{robots[0]}").json()["status"]["policy_version"] == 2

    back = fleet.c.post(
        f"/v1/projects/{fleet.project}/rollouts",
        {"spec": {"kind": "rollback", "robot_ids": robots}},
    )
    assert back.status_code == 201, back.text
    bid = back.json()["metadata"]["id"]
    assert back.json()["status"]["state"] == "awaiting_approval"
    assert back.json()["status"]["revision_id"] == revision
    assert back.json()["status"]["name"] == "pick v1"
    fleet.switched(bid, revision, robots)
    assert fleet.rollout(bid)["status"]["state"] == "live"
    assert fleet.c.get(f"/v1/robots/{robots[0]}").json()["status"]["policy_version"] == 1
    listed = fleet.c.get(f"/v1/projects/{fleet.project}/rollouts").json()["items"]
    assert [r["metadata"]["id"] for r in listed] == [bid, sid, rid]


def test_an_inferior_shadow_stops_the_rollout(production: Fleet) -> None:
    fleet = production
    v1 = fleet.push("v1", "a")["metadata"]["id"]
    robots = fleet.robots[:1]
    rid = fleet.deploy(v1, robots)["metadata"]["id"]
    fleet.ack(rid, "Succeeded", result=measured(40))
    revision = fleet.ack(rid, "Succeeded", result=measured(40))["status"]["revision_id"]
    fleet.switched(rid, revision, robots)
    v2 = fleet.push("v2", "b")["metadata"]["id"]
    sid = fleet.deploy(v2, robots)["metadata"]["id"]  # production: a 10 min shadow by default
    fleet.ack(sid, "Succeeded", result=measured(40))
    assert fleet.ack(sid, "Succeeded", result=measured(40))["status"]["state"] == "shadow_testing"
    rollout = fleet.ack(
        sid,
        "Succeeded",
        result={"evaluation": {"kind": "shadow", "verdict": "inferior", "samples": 900}},
    )
    assert rollout["status"]["state"] == "stopped"
    assert rollout["status"]["reason"] == "ROLLOUT_SHADOW_INFERIOR"
    assert steps(rollout)["shadow"] == "failed"
    assert steps(rollout)["approve"] == "skipped"


def test_a_failed_prepare_stops_with_the_sites_reason(bench: Fleet) -> None:
    version = bench.push("v1", "a")["metadata"]["id"]
    rid = bench.deploy(version, bench.robots[:1])["metadata"]["id"]
    rollout = bench.ack(
        rid,
        "Failed",
        reason=None,
        message="the artifact is not signed by a key this site trusts",
    )
    assert rollout["status"]["state"] == "stopped"
    assert rollout["status"]["reason"] == "ROLLOUT_PREPARE_FAILED"
    assert "not signed by a key this site trusts" in rollout["status"]["message"]
    activity = bench.c.get(f"/v1/projects/{bench.project}/activity").json()["items"][0]
    assert activity["level"] == "error"
    assert "stopped" in activity["summary"]


def test_a_gpu_too_slow_for_the_robots_stops_at_prepare(bench: Fleet) -> None:
    version = bench.push("v1", "a")["metadata"]["id"]
    rid = bench.deploy(version, bench.robots)["metadata"]["id"]
    rollout = bench.ack(rid, "Succeeded", result=measured(2500))
    assert rollout["status"]["state"] == "stopped"
    assert rollout["status"]["reason"] == "ROLLOUT_TIMING_UNFIT"
    assert "faster GPU" in rollout["status"]["message"]


def test_one_rollout_at_a_time_and_cancel(bench: Fleet) -> None:
    version = bench.push("v1", "a")["metadata"]["id"]
    rid = bench.deploy(version, bench.robots[:2])["metadata"]["id"]
    clash = bench.c.post(
        f"/v1/projects/{bench.project}/rollouts",
        {"spec": {"robot_ids": bench.robots[1:], "policy_version_id": version}},
    )
    assert clash.status_code == 409
    assert "still in progress for arm-2" in clash.text
    cancelled = bench.c.post(f"/v1/rollouts/{rid}/cancel", {})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"]["state"] == "cancelled"
    again = bench.c.post(f"/v1/rollouts/{rid}/cancel", {})
    assert again.status_code == 409


def test_try_is_for_bench_sites_only(production: Fleet) -> None:
    version = production.push("v1", "a")["metadata"]["id"]
    response = production.c.post(
        f"/v1/projects/{production.project}/rollouts",
        {"spec": {"kind": "try", "robot_ids": production.robots[:1], "policy_version_id": version}},
    )
    assert response.status_code == 409
    assert "production site" in response.text


def test_nothing_to_roll_back_to(bench: Fleet) -> None:
    response = bench.c.post(
        f"/v1/projects/{bench.project}/rollouts",
        {"spec": {"kind": "rollback", "robot_ids": bench.robots[:1]}},
    )
    assert response.status_code == 409
    assert "no version to go back to" in response.text


def test_production_servers_install_with_one_line(
    make_api: AppFactory, settings: Any, tmp_path: Path
) -> None:
    # Without an agent release to offer, there is no one-line installer.
    plain, _ = make_api()
    assert plain.get("/install.sh", token=False).status_code == 404
    keys = tmp_path / "release-keys.pem"
    keys.write_text(
        "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAexample\n-----END PUBLIC KEY-----\n"
    )
    client, org = make_api(
        settings_overrides={
            "public_url": "https://neura.example",
            "agent_release_version": "1.2.3",
            "agent_release_url": "https://releases.neura.example/neura-agent-1.2.3.tar",
            "agent_release_sha256": "ab" * 32,
            "agent_release_keys_file": keys,
        }
    )
    script = client.get("/install.sh", token=False)  # before anyone signs in
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/x-shellscript")
    assert f"BUNDLE_SHA256={'ab' * 32}" in script.text
    assert "BUNDLE_URL=https://releases.neura.example/neura-agent-1.2.3.tar" in script.text
    assert "MCowBQYDK2VwAyEAexample" in script.text
    fleet = Fleet(client, org, str(settings.database_url), "production")
    token = client.post(f"/v1/sites/{fleet.site}/enrollment-tokens", {}).json()
    assert token["install_command"] == (
        f"curl -fsSL https://neura.example/install.sh | sudo sh -s -- --token {token['secret']}"
    )
    assert token["enroll_command"].startswith("neura-agent enroll --control-plane ")


def test_install_commands_trust_the_workspaces_keys(bench: Fleet) -> None:
    public = signing.raw_public_key(bench.key).hex()
    key = bench.c.post(
        f"/v1/organizations/{bench.org}/publisher-keys",
        {"public_key": public, "display_name": "CI signing key"},
    )
    assert key.status_code == 201, key.text
    assert (
        bench.c.post(
            f"/v1/organizations/{bench.org}/publisher-keys",
            {"public_key": public, "display_name": "again"},
        ).status_code
        == 200
    )
    token = bench.c.post(f"/v1/sites/{bench.site}/enrollment-tokens", {}).json()
    assert token["install_command"].startswith("neura-agent enroll --control-plane ")
    assert token["join_command"].startswith("neura site join plant-1 --control-plane ")
    assert f"--trust {public}" in token["join_command"]
    assert token["join_command"].endswith(" --bench")
    assert token["enroll_command"].startswith("neura-agent enroll --control-plane ")


def test_the_cli_signs_in_through_the_browser(make_api: AppFactory) -> None:
    issuer = OidcIssuer()
    client, org = make_api(oidc_verifier=issuer.verifier, memberships={ADMIN: Role.ADMIN})
    anonymous = client.as_token(None)
    started = anonymous.post("/v1/login-sessions", {"client_name": "neura CLI on ada-laptop"})
    assert started.status_code == 201, started.text
    login = started.json()
    assert login["verification_url"].endswith(f"/activate?code={login['user_code']}")
    exchange = f"/v1/login-sessions/{login['id']}/token"
    pending = anonymous.post(exchange, {"secret": login["secret"]})
    assert pending.status_code == 409
    assert pending.json()["reason"] == "API_LOGIN_PENDING"
    assert anonymous.post(exchange, {"secret": "x" * 43}).status_code == 404

    # An API token cannot approve: only a signed-in person can mint a token for a CLI.
    refused = client.post("/v1/login-approvals", {"user_code": login["user_code"]})
    assert refused.status_code == 403
    ada = client.as_token(issuer.token("ada"))
    approved = ada.post("/v1/login-approvals", {"user_code": login["user_code"].replace("-", "")})
    assert approved.status_code == 200, approved.text
    assert approved.json() == {
        "client_name": "neura CLI on ada-laptop",
        "organization_id": org,
        "scopes": ["admin"],
    }
    issued = anonymous.post(exchange, {"secret": login["secret"]})
    assert issued.status_code == 200, issued.text
    cli = client.as_token(issued.json()["token"])
    assert cli.get("/v1/workspace").json()["organization_id"] == org
    assert anonymous.post(exchange, {"secret": login["secret"]}).status_code == 404  # once


def report_inventory(database_url: str, site_id: str, robots: list[dict[str, Any]]) -> None:
    """What a site's agent sends: the robots its server drives."""
    import asyncio
    import json

    from neura.v1 import site_pb2
    from neura_control.db.engine import create_engine, session_factory
    from neura_control.services import inventory

    message = site_pb2.RobotInventory(
        robots=[
            site_pb2.DiscoveredRobot(
                robot_profile_json=json.dumps(contract("so101.robot.yaml")), **robot
            )
            for robot in robots
        ]
    )

    async def run() -> None:
        engine = create_engine(database_url, pool_size=1)
        try:
            async with session_factory(engine).begin() as session:
                await inventory.report(session, site_id, message)
        finally:
            await engine.dispose()

    asyncio.run(run())


def manifest_robots(database_url: str, site_id: str) -> list[dict[str, Any]]:
    import asyncio

    from neura_control.db.engine import create_engine, session_factory
    from neura_control.db.models import Site
    from neura_control.services.manifests import manifest_content

    async def run() -> list[dict[str, Any]]:
        engine = create_engine(database_url, pool_size=1)
        try:
            async with session_factory(engine).begin() as session:
                site = await session.get(Site, site_id)
                assert site is not None
                content = await manifest_content(session, site)
                assert content["site"]["kind"] in ("bench", "production")
                robots: list[dict[str, Any]] = content["robots"]
                return robots
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_robots_a_site_finds_are_registered_and_kept(bench: Fleet) -> None:
    found: list[dict[str, Any]] = [
        {"local_name": "sim-1", "driver": "simulated", "simulated": True, "state": "ready"},
        {"local_name": "sim-2", "driver": "simulated", "simulated": True, "state": "ready"},
        {
            "local_name": "so101-1",
            "driver": "so101",
            "port": "/dev/serial/by-id/usb-1a86_x",
            "state": "needs_setup",
            "message": "add it to the robots file",
        },
    ]
    report_inventory(bench.db, bench.site, found)
    report_inventory(bench.db, bench.site, found)  # the same inventory again changes nothing
    robots = bench.c.get(f"/v1/projects/{bench.project}/robots").json()["items"]
    discovered = {r["spec"]["name"]: r for r in robots if r["status"]["discovered"]}
    assert sorted(discovered) == ["sim-1", "sim-2", "so101-1"]
    assert discovered["sim-1"]["status"]["hardware"]["local_name"] == "sim-1"
    assert discovered["sim-1"]["status"]["phase"] == "Ready"
    assert discovered["so101-1"]["status"]["phase"] == "NeedsAttention"
    assert discovered["so101-1"]["status"]["message"] == "add it to the robots file"
    local = {r["name"]: r.get("local_name") for r in manifest_robots(bench.db, bench.site)}
    assert local["sim-1"] == "sim-1"
    assert local["arm-1"] is None  # created through the API, not found by the agent

    # A robot the server no longer reports is kept, shown offline.
    report_inventory(bench.db, bench.site, found[:1])
    gone = bench.c.get(f"/v1/robots/{discovered['sim-2']['metadata']['id']}").json()["status"]
    assert gone["phase"] == "Offline"
    summaries = [
        a["summary"] for a in bench.c.get(f"/v1/projects/{bench.project}/activity").json()["items"]
    ]
    assert "sim-2 is no longer connected to plant-1's server" in summaries
    assert "plant-1 found a simulated so101-follower: sim-1" in summaries

    # Another site's sim-1 does not take this one's name.
    other = bench.c.post(
        f"/v1/projects/{bench.project}/sites",
        {"spec": {"name": "lab", "display_name": "Lab", "offline_authorization_hours": 24}},
    ).json()["metadata"]["id"]
    report_inventory(bench.db, other, found[:1])
    names = {
        r["spec"]["name"]
        for r in bench.c.get(f"/v1/projects/{bench.project}/robots").json()["items"]
    }
    assert "lab-sim-1" in names


def test_people_on_their_own_get_a_workspace_only_when_self_signup_is_on(
    make_api: AppFactory,
) -> None:
    issuer = OidcIssuer()

    def grace() -> str:
        return issuer.token("grace", email="Grace@Example.com", email_verified=True)

    # A company's deployment: SSO alone gets no one in; an admin adds people.
    closed, _ = make_api(oidc_verifier=issuer.verifier)
    assert closed.as_token(grace()).get("/v1/workspace").status_code != 200

    # Neura's cloud for individuals: the first sign-in makes a workspace they administer.
    open_, _ = make_api(oidc_verifier=issuer.verifier, settings_overrides={"self_signup": True})
    first = open_.as_token(grace()).get("/v1/workspace")
    assert first.status_code == 200, first.text
    assert first.json()["display_name"] == "grace's workspace"
    assert set(first.json()["permissions"]) == {"admin", "deploy", "read"}
    again = open_.as_token(grace()).get("/v1/workspace").json()
    assert again["organization_id"] == first.json()["organization_id"]  # once, not per sign-in
    me = open_.as_token(grace()).get("/v1/me").json()
    assert [o["id"] for o in me["organizations"]] == [first.json()["organization_id"]]
