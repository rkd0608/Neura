"""End to end: control plane + site agent + gateway + robots (milestone M4 exit criteria).

A clean server enrolls with a one-time token, receives a signed desired state, prepares a revision,
waits for local approval, and its robots switch revisions at a task boundary. The site keeps
operating through a control-plane outage and reconnects afterwards.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from functools import partial
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient

from conftest import AppFactory, ConformanceClient, contract
from neura_agent.gateway import InProcessGateway
from neura_agent.runner import AgentRuntime, EnrollmentError
from neura_agent.settings import AgentSettings
from neura_contracts import parse_document, signing
from neura_control.app import create_app
from neura_runtime.grpc_client import RobotCredentials
from neura_sim.grpc_robots import AssignedRobot

ROBOTS = ["robot-0", "robot-1"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class BackgroundLoop:
    """An asyncio loop on its own thread, for the site side of the test."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro: Coroutine[Any, Any, Any], timeout: float = 30) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def close(self) -> None:
        async def drain() -> None:
            current = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not current]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        self.run(drain(), timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def wait_for(condition: Callable[[], Any], timeout: float = 30, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = condition()
        if last:
            return last
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}; last value: {last!r}")


@pytest.fixture
def site_loop() -> Iterator[BackgroundLoop]:
    loop = BackgroundLoop()
    yield loop
    loop.close()


# The customer's release key: the artifact is registered with its signature and the site trusts it.
PUBLISHER = signing.load_private_key(signing.generate_key())


def setup_customer(client: ConformanceClient, org: str) -> dict[str, Any]:
    project = client.post(
        f"/v1/organizations/{org}/projects",
        {"spec": {"name": "packing", "display_name": "Packing"}},
    ).json()["metadata"]["id"]
    site = client.post(
        f"/v1/projects/{project}/sites",
        {
            "spec": {
                "name": "warehouse",
                "display_name": "Warehouse",
                "offline_authorization_hours": 1,
            }
        },
    ).json()["metadata"]["id"]
    rp = client.post(
        f"/v1/projects/{project}/robot-profiles", {"document": contract("so101.robot.yaml")}
    ).json()
    artifact = contract("act-so101.artifact.yaml")
    signature = signing.sign(parse_document(artifact).digest(), PUBLISHER)
    art = client.post(
        f"/v1/projects/{project}/model-artifacts",
        {"document": artifact, "signatures": [signature]},
    ).json()
    assert art["signatures"] == [signature]
    exp = client.post(
        f"/v1/projects/{project}/execution-profiles",
        {"document": contract("act-so101-30hz.profile.yaml")},
    ).json()
    # A second execution profile (another refill threshold) gives a second revision to switch to.
    profile2 = contract("act-so101-30hz.profile.yaml")
    profile2["metadata"]["version"] = "example-2"
    profile2["spec"]["chunk"]["refill_threshold_actions"] = 25
    exp2 = client.post(f"/v1/projects/{project}/execution-profiles", {"document": profile2}).json()
    robots = [
        client.post(
            f"/v1/projects/{project}/robots",
            {
                "spec": {
                    "name": name,
                    "display_name": name,
                    "site_id": site,
                    "robot_profile_id": rp["metadata"]["id"],
                }
            },
        ).json()["metadata"]["id"]
        for name in ROBOTS
    ]
    deployment = client.post(
        f"/v1/projects/{project}/deployments",
        {
            "spec": {
                "name": "pilot",
                "display_name": "Pilot",
                "site_id": site,
                "robot_ids": robots,
                "rollout": {"strategy": "all_at_task_boundary"},
            }
        },
    ).json()["metadata"]["id"]

    def revision(execution_profile: dict[str, Any]) -> str:
        response = client.post(
            f"/v1/deployments/{deployment}/revisions",
            {
                "spec": {
                    "model_artifact_id": art["metadata"]["id"],
                    "execution_profile_id": execution_profile["metadata"]["id"],
                    "robot_profile_id": rp["metadata"]["id"],
                }
            },
        )
        assert response.status_code == 201, response.text
        rid: str = response.json()["metadata"]["id"]
        return rid

    return {
        "project": project,
        "site": site,
        "robots": robots,
        "deployment": deployment,
        "rev1": revision(exp),
        "rev2": revision(exp2),
    }


def operation_state(client: ConformanceClient, operation_id: str) -> str:
    state: str = client.get(f"/v1/operations/{operation_id}").json()["state"]
    return state


def succeeded(client: ConformanceClient, operation_id: str) -> bool:
    return operation_state(client, operation_id) == "Succeeded"


@pytest.mark.parametrize("telemetry_mode", ["standard", "minimal"])
def test_enroll_deploy_approve_switch_and_survive_outage(
    make_api: AppFactory,
    site_loop: BackgroundLoop,
    tmp_path: Path,
    settings: Any,
    telemetry_mode: Literal["standard", "minimal"],
) -> None:
    agent_port = free_port()
    endpoint = f"127.0.0.1:{agent_port}"
    overrides = {
        "agent_grpc_listen": endpoint,
        "agent_endpoint": endpoint,
        "agent_poll_interval_s": 0.2,
        "manifest_signing_key": "x" * 43,
    }
    client, org = make_api(settings_overrides=overrides)
    ids = setup_customer(client, org)

    # --- enrollment ------------------------------------------------------------------------
    token = client.post(f"/v1/sites/{ids['site']}/enrollment-tokens", {}).json()["secret"]
    agent_settings = AgentSettings(
        state_dir=tmp_path / "agent",
        development=True,
        control_plane_insecure=True,
        gateway_listen="127.0.0.1:0",
        gateway_status_listen=None,
        status_interval_s=0.3,
        reconnect_max_backoff_s=0.5,
        # Robots connect with mutual TLS: certificates from the site CA, issued into a directory
        # the simulated robots read (development only; real robots send CSRs).
        dev_robot_certificate_dir=tmp_path / "robot-certs",
        activation_timeout_s=60,
        gateway_backend="simulated",
        trusted_publisher_keys=[signing.raw_public_key(PUBLISHER).hex()],
        telemetry_mode=telemetry_mode,
    )
    gateway = InProcessGateway()
    agent = AgentRuntime(agent_settings, gateway=gateway, command_runner=lambda args: None)
    identity = site_loop.run(agent.enroll(endpoint, token))
    with pytest.raises(EnrollmentError):  # the token was single use
        site_loop.run(
            AgentRuntime(
                agent_settings.model_copy(update={"state_dir": tmp_path / "b"}),
                command_runner=lambda args: None,
            ).enroll(endpoint, token)
        )
    listed = client.get(f"/v1/sites/{ids['site']}/servers")
    assert listed.status_code == 200, listed.text
    servers = listed.json()["items"]
    assert [s["metadata"]["id"] for s in servers] == [identity.server_id]
    assert {c["name"] for c in servers[0]["status"]["preflight"]} >= {
        "nvidia_driver",
        "state_dir_writable",
    }

    site_loop.run(agent.start())
    site = wait_for(
        lambda: (s := client.get(f"/v1/sites/{ids['site']}").json())["status"]["connected"] and s,
        what="site connected",
    )
    assert site["status"]["agent_version"]
    assert agent.core is not None
    wait_for(
        lambda: agent.core is not None and agent.core.state.manifest_generation > 0, what="manifest"
    )
    # Signed contact proofs keep renewing offline authorization while the site is connected.
    first_contact = agent.core.state.last_contact_unix
    wait_for(
        lambda: agent.core is not None and agent.core.state.last_contact_unix != first_contact,
        what="contact proof",
    )

    # --- prepare: verified at the site ---------------------------------------------------------
    for rev in ("rev1", "rev2"):
        op: str = client.post(f"/v1/revisions/{ids[rev]}/prepare").json()["metadata"]["id"]
        wait_for(partial(succeeded, client, op), what=f"prepare {rev}")
        status = client.get(f"/v1/revisions/{ids[rev]}").json()["status"]
        assert status["phase"] == "Prepared"
        assert status["capacity"]["max_robots"] >= len(ROBOTS)  # "this server can run N robots"

    # --- robots start, but nothing is assigned until a local approval ---------------------------
    # Robots ask the gateway what to run, over the same mutual-TLS channel they are served on.
    robots = [
        AssignedRobot(
            name,
            gateway=f"127.0.0.1:{gateway.server.bound_port}",  # type: ignore[union-attr]
            credentials=RobotCredentials.from_directory(tmp_path / "robot-certs", name),
            task_seconds=1.5,
            poll_seconds=0.2,
        )
        for name in ROBOTS
    ]
    from neura_agent.app import create_app as create_agent_app

    local = TestClient(create_agent_app(agent_settings, runtime=agent, start_probe=False))
    local.__enter__()
    try:
        stop = asyncio.Event()
        running = [site_loop.spawn(r.run(stop)) for r in robots]
        time.sleep(1.0)
        assert all(r.process is None for r in robots)

        activation = client.post(
            f"/v1/deployments/{ids['deployment']}/activation-requests", {"revision_id": ids["rev1"]}
        ).json()["metadata"]["id"]
        wait_for(
            lambda: operation_state(client, activation) == "Running", what="activation running"
        )
        pending = wait_for(
            lambda: local.get("/v1/local/approvals").json()["items"], what="pending approval"
        )
        # The cloud cannot approve; only the local operator can.
        approved = local.post(
            f"/v1/local/approvals/{pending[0]['command_id']}/approve", json={"operator": "ops@site"}
        )
        assert approved.status_code == 403  # no operator credential configured on this agent
        site_loop.run(agent.core.approve(pending[0]["command_id"], "ops@site"))

        wait_for(
            lambda: operation_state(client, activation) == "Succeeded", what="activation succeeded"
        )
        deployment = client.get(f"/v1/deployments/{ids['deployment']}").json()["status"]
        assert set(deployment["active_revision_ids"].values()) == {ids["rev1"]}
        request = client.get(f"/v1/deployments/{ids['deployment']}/activation-requests").json()[
            "items"
        ][0]
        # Minimal telemetry keeps the operator's name at the site.
        approver = "ops@site" if telemetry_mode == "standard" else None
        assert (request["state"], request["approved_by"]) == ("Activated", approver)
        wait_for(
            lambda: all(
                r.process is not None and r.process.runtime.dispatched > 30 for r in robots
            ),
            what="robots dispatching",
        )

        def live_sessions() -> list[dict[str, Any]]:
            response = client.get(f"/v1/projects/{ids['project']}/sessions")
            assert response.status_code == 200, response.text
            items: list[dict[str, Any]] = response.json()["items"]
            return items

        sessions = wait_for(live_sessions, what="sessions")
        assert {s["revision_id"] for s in sessions} == {ids["rev1"]}
        assert set(sessions[0]["metrics"]) <= {
            "chunks",
            "dispatched_actions",
            "buffer_actions",
            "rtt_p99_ms",
        }
        wait_for(
            lambda: (
                client.get(f"/v1/robots/{ids['robots'][0]}").json()["status"]["phase"]
                in ("Running", "Ready")
            ),
            what="robot phase reported",
        )

        # --- switch revisions at a task boundary ------------------------------------------------
        second = client.post(
            f"/v1/deployments/{ids['deployment']}/activation-requests", {"revision_id": ids["rev2"]}
        ).json()["metadata"]["id"]
        pending = wait_for(
            lambda: local.get("/v1/local/approvals").json()["items"], what="second approval"
        )
        site_loop.run(agent.core.approve(pending[0]["command_id"], "ops@site"))
        wait_for(
            lambda: operation_state(client, second) == "Succeeded",
            timeout=40,
            what="switch to rev2",
        )
        assert all(r.process is not None and r.process.revision_id == ids["rev2"] for r in robots)
        assert all(r.switches == 2 for r in robots)
        assert all(r.process is not None and r.process.runtime.fault is None for r in robots)
        deployment = client.get(f"/v1/deployments/{ids['deployment']}").json()["status"]
        assert set(deployment["active_revision_ids"].values()) == {ids["rev2"]}

        # --- control-plane outage: the site keeps operating ---------------------------------------
        client.client.__exit__(None, None, None)
        wait_for(
            lambda: agent.connection is not None and not agent.connection.connected,
            what="disconnect",
        )
        before = {r.name: r.process.runtime.dispatched for r in robots if r.process}
        time.sleep(2.0)
        after = {r.name: r.process.runtime.dispatched for r in robots if r.process}
        assert all(after[n] > before[n] + 20 for n in before), (before, after)
        assert agent.core.offline_remaining_s() is not None
        assert not agent.core.authorization_expired()

        # --- the control plane comes back; the agent reconnects by itself --------------------
        restarted = TestClient(
            create_app(settings.model_copy(update=overrides)), raise_server_exceptions=False
        )
        restarted.__enter__()
        try:
            reconnected = ConformanceClient(restarted, client.token)
            wait_for(
                lambda: reconnected.get(f"/v1/sites/{ids['site']}").json()["status"]["connected"],
                what="reconnected",
            )
        finally:
            restarted.__exit__(None, None, None)

        stop.set()
        for future in running:
            future.result(timeout=10)

        # --- everything the site sent is in its outbound ledger, chained (design §18.2) -------
        sent = agent.outbound.ledger.summary()
        assert {"enroll", "hello", "facts", "status", "operation"} <= set(sent)
        assert agent.outbound.ledger.verify().ok
        assert agent.outbound.unknown_fields == 0
        if telemetry_mode == "minimal":
            # Health, versions, identifiers, states and reason codes only: no metrics, no free
            # text, no host name. Deploying, approving and switching still worked above.
            assert "telemetry" not in sent
            entries = json.dumps(agent.outbound.ledger.entries(limit=10_000))
            assert '"metrics"' not in entries
            assert '"hostname"' not in entries
            assert '"approved_by"' not in entries
        else:
            assert "telemetry" in sent
    finally:
        local.__exit__(None, None, None)
        site_loop.run(agent.stop())
