"""End to end, shadow serving (design §28.3): the control plane, a real site agent and gateway,
and simulated robots served over mutual TLS.

The robots run the base revision. The deployment's release gate needs a shadow evaluation: the
candidate is served on the robots' live observations for a few seconds, its actions compared with
the running model's and never sent. Activation is refused before the evaluation and accepted
after it, and the robots are served throughout as if nothing were shadowing them.
"""

from __future__ import annotations

import asyncio
import json
import time
from functools import partial
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("lerobot.datasets.lerobot_dataset", exc_type=ImportError)

from fastapi.testclient import TestClient
from test_site_agent_e2e import BackgroundLoop, free_port, wait_for

from conftest import AppFactory
from neura_agent.app import create_app as create_agent_app
from neura_agent.gateway import InProcessGateway
from neura_agent.runner import AgentRuntime
from neura_agent.settings import AgentSettings
from neura_bench.deployments import synthetic
from neura_contracts import parse_document, signing
from neura_runtime.grpc_client import RobotCredentials
from neura_sim.grpc_robots import AssignedRobot

PUBLISHER = signing.load_private_key(signing.generate_key())
ROBOTS = ["robot-0", "robot-1"]


@pytest.fixture
def site_loop() -> Any:
    loop = BackgroundLoop()
    yield loop
    loop.close()


def register(c: Any, project: str, files: Any) -> tuple[str, str]:
    document = json.loads(files.artifact.read_text())
    artifact = c.post(
        f"/v1/projects/{project}/model-artifacts",
        {
            "document": document,
            "signatures": [signing.sign(parse_document(document).digest(), PUBLISHER)],
        },
    )
    assert artifact.status_code == 201, artifact.text
    profile = c.post(
        f"/v1/projects/{project}/execution-profiles",
        {"document": json.loads(files.execution_profile.read_text())},
    )
    assert profile.status_code == 201, profile.text
    return artifact.json()["metadata"]["id"], profile.json()["metadata"]["id"]


def test_a_candidate_shadows_live_robots_before_it_may_replace_their_model(
    make_api: AppFactory, settings: Any, site_loop: BackgroundLoop, tmp_path: Path
) -> None:
    endpoint = f"127.0.0.1:{free_port()}"
    c, org = make_api(
        settings_overrides={
            "agent_grpc_listen": endpoint,
            "agent_endpoint": endpoint,
            "agent_poll_interval_s": 0.2,
            "manifest_signing_key": "x" * 43,
        }
    )
    base_files = synthetic(tmp_path / "base", model="act", size="small", image=(96, 128), seed=0)
    candidate_files = synthetic(
        tmp_path / "candidate", model="act", size="small", image=(96, 128), seed=1
    )
    project = c.post(
        f"/v1/organizations/{org}/projects", {"spec": {"name": "cell", "display_name": "Cell"}}
    ).json()["metadata"]["id"]
    site = c.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "lab", "display_name": "Lab", "offline_authorization_hours": 1}},
    ).json()["metadata"]["id"]
    robot_profile = c.post(
        f"/v1/projects/{project}/robot-profiles",
        {"document": json.loads(base_files.robot_profile.read_text())},
    ).json()["metadata"]["id"]
    robots = [
        c.post(
            f"/v1/projects/{project}/robots",
            {
                "spec": {
                    "name": name,
                    "display_name": name,
                    "site_id": site,
                    "robot_profile_id": robot_profile,
                }
            },
        ).json()["metadata"]["id"]
        for name in ROBOTS
    ]
    deployment = c.post(
        f"/v1/projects/{project}/deployments",
        {
            "spec": {
                "name": "pilot",
                "display_name": "Pilot",
                "site_id": site,
                "robot_ids": robots,
                "rollout": {"strategy": "all_at_task_boundary"},
                "release_gates": {"evaluations": ["shadow"]},
            }
        },
    ).json()["metadata"]["id"]

    def revision(files: Any) -> str:
        artifact, profile = register(c, project, files)
        created = c.post(
            f"/v1/deployments/{deployment}/revisions",
            {
                "spec": {
                    "model_artifact_id": artifact,
                    "execution_profile_id": profile,
                    "robot_profile_id": robot_profile,
                }
            },
        )
        assert created.status_code == 201, created.text
        revision_id: str = created.json()["metadata"]["id"]
        return revision_id

    base, candidate = revision(base_files), revision(candidate_files)

    agent_settings = AgentSettings(
        state_dir=tmp_path / "agent",
        development=True,
        control_plane_insecure=True,
        gateway_listen="127.0.0.1:0",
        gateway_status_listen=None,
        status_interval_s=0.3,
        reconnect_max_backoff_s=0.5,
        dev_robot_certificate_dir=tmp_path / "robot-certs",
        activation_timeout_s=60,
        gateway_backend="simulated",
        trusted_publisher_keys=[signing.raw_public_key(PUBLISHER).hex()],
    )
    gateway = InProcessGateway()
    agent = AgentRuntime(agent_settings, gateway=gateway, command_runner=lambda args: None)
    token = c.post(f"/v1/sites/{site}/enrollment-tokens", {}).json()["secret"]
    site_loop.run(agent.enroll(endpoint, token))
    site_loop.run(agent.start())
    local = TestClient(create_agent_app(agent_settings, runtime=agent, start_probe=False))
    local.__enter__()
    stop = asyncio.Event()
    running: list[Any] = []
    try:
        core = agent.core
        assert core is not None
        wait_for(lambda: core.state.manifest_generation > 0, what="the site's manifest")
        for files in (base_files, candidate_files):
            assert files.checkpoint is not None
            core.artifacts.import_directory(files.checkpoint)

        def finished(operation_id: str) -> dict[str, Any] | None:
            operation: dict[str, Any] = c.get(f"/v1/operations/{operation_id}").json()
            done = operation["state"] in ("Succeeded", "Failed", "Cancelled")
            return operation if done else None

        def activate(revision_id: str) -> dict[str, Any]:
            requested = c.post(
                f"/v1/deployments/{deployment}/activation-requests", {"revision_id": revision_id}
            )
            assert requested.status_code == 202, requested.text
            pending = wait_for(
                lambda: local.get("/v1/local/approvals").json()["items"], what="approval"
            )
            site_loop.run(core.approve(pending[0]["command_id"], "ops@site"))
            operation_id = requested.json()["metadata"]["id"]
            done: dict[str, Any] = wait_for(
                lambda: finished(operation_id), timeout=60, what="activation"
            )
            return done

        for revision_id in (base, candidate):
            prepared: str = c.post(f"/v1/revisions/{revision_id}/prepare").json()["metadata"]["id"]
            outcome = wait_for(partial(finished, prepared), what="prepare")
            assert outcome["state"] == "Succeeded", outcome
        assigned = [
            AssignedRobot(
                name,
                gateway=f"127.0.0.1:{gateway.server.bound_port}",  # type: ignore[union-attr]
                credentials=RobotCredentials.from_directory(tmp_path / "robot-certs", name),
                task_seconds=2,
                poll_seconds=0.2,
            )
            for name in ROBOTS
        ]
        running = [site_loop.spawn(r.run(stop)) for r in assigned]
        assert activate(base)["state"] == "Succeeded"  # a first activation needs no evidence
        wait_for(
            lambda: all(
                r.process is not None and r.process.runtime.dispatched > 30 for r in assigned
            ),
            what="robots served",
        )

        # The gate: the candidate may not replace the running model without a shadow.
        blocked = c.post(
            f"/v1/deployments/{deployment}/activation-requests", {"revision_id": candidate}
        )
        assert blocked.status_code == 409
        assert "shadow evaluation" in blocked.json()["detail"]

        served_before = {
            r.name: r.process.runtime.counters["chunks_accepted"]  # type: ignore[union-attr]
            for r in assigned
        }
        started = c.post(
            f"/v1/deployments/{deployment}/evaluations",
            {
                "spec": {
                    "candidate_revision_id": candidate,
                    "suite": {
                        "kind": "shadow",
                        "duration_s": 60,
                        "min_samples": 5,
                        "max_samples": 5,  # stop as soon as five are compared
                        "tolerance": 0.2,
                    },
                }
            },
        )
        assert started.status_code == 202, started.text
        seen_shadowing = seen_locally = False
        deadline = time.monotonic() + 60
        while (operation := finished(started.json()["metadata"]["id"])) is None:
            assert time.monotonic() < deadline, "the shadow evaluation did not finish"
            status = gateway.server.status() if gateway.server is not None else {}
            seen_shadowing |= candidate in status.get("shadows", {})
            # The site's operator sees it in the agent's local status.
            seen_locally |= {"candidate": candidate, "of": base} in core.jobs.local_status()[
                "shadows"
            ]
            time.sleep(0.2)
        assert operation["state"] == "Succeeded", operation
        assert seen_shadowing  # the gateway served the candidate while it ran
        assert seen_locally
        evaluation = c.get(f"/v1/evaluations/{started.json()['target']}").json()
        assert evaluation["spec"]["baseline_revision_id"] == base
        report = evaluation["status"]["report"]
        assert report["kind"] == "shadow"
        assert report["samples"] >= 5
        assert report["robots"] == len(ROBOTS)
        assert report["candidate"] == {"invalid_outputs": 0, "failed": 0, "out_of_range": 0}
        assert set(report["divergence"]) == {"mean", "p95", "max"}
        assert evaluation["status"]["verdict"] == "non_inferior", report
        # The shadow is gone from the gateway, and the robots were served throughout.
        assert gateway.server is not None
        assert gateway.server.status()["shadows"] == {}
        for robot in assigned:
            assert robot.process is not None
            assert robot.process.runtime.counters["chunks_accepted"] > served_before[robot.name]
            assert robot.process.runtime.state.value != "Faulted"
        sessions = gateway.server.status()["sessions"]
        assert {s["revision_id"] for s in sessions} == {base}
        assert all(not s["rejections"] for s in sessions), sessions

        assert activate(candidate)["state"] == "Succeeded"
    finally:
        stop.set()
        for task in running:
            task.result(timeout=30)
        local.__exit__(None, None, None)
        site_loop.run(agent.stop())
