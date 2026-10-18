"""End to end, the improvement loop (design §28): the control plane, a real site agent, and
``neura-learn`` in the agent's worker sandbox, on a real (synthetic-weight) ACT model.

The site holds episodes its robots recorded. The control plane asks it to fine-tune the running
model on the successful ones; the site trains, signs the result and reports its manifest, never
an episode; the control plane registers it and adds a candidate revision. The deployment's
release gate holds the candidate back until the site has evaluated it against the running model
on the recorded episodes, and then lets it through.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("lerobot.datasets.lerobot_dataset", exc_type=ImportError)

from sqlalchemy import update
from test_site_agent_e2e import BackgroundLoop, free_port, wait_for

from conftest import AppFactory
from neura_agent.gateway import InProcessGateway
from neura_agent.runner import AgentRuntime
from neura_agent.settings import AgentSettings
from neura_bench.deployments import synthetic
from neura_contracts import parse_document, signing
from neura_control.db.engine import create_engine, session_factory
from neura_control.db.models import Deployment, Revision
from neura_runtime.capture.episode import canonical
from neura_runtime.capture.testing import synthetic_episode

PUBLISHER = signing.load_private_key(signing.generate_key())


def db(settings: Any, statement: Any) -> None:
    async def run() -> None:
        engine = create_engine(str(settings.database_url), pool_size=1)
        try:
            async with session_factory(engine).begin() as session:
                await session.execute(statement)
        finally:
            await engine.dispose()

    asyncio.run(run())


def lineage_weights(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    weights: list[dict[str, Any]] = artifact["document"]["spec"]["weights"]
    return weights


@pytest.fixture
def site_loop() -> Any:
    loop = BackgroundLoop()
    yield loop
    loop.close()


def test_the_site_improves_its_model_and_the_gate_lets_it_through(
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
    files = synthetic(tmp_path / "model", model="act", size="small", image=(96, 128))
    project = c.post(
        f"/v1/organizations/{org}/projects", {"spec": {"name": "cell", "display_name": "Cell"}}
    ).json()["metadata"]["id"]
    site = c.post(
        f"/v1/projects/{project}/sites",
        {"spec": {"name": "lab", "display_name": "Lab", "offline_authorization_hours": 1}},
    ).json()["metadata"]["id"]
    robot_profile = c.post(
        f"/v1/projects/{project}/robot-profiles",
        {"document": json.loads(files.robot_profile.read_text())},
    ).json()["metadata"]["id"]
    document = json.loads(files.artifact.read_text())
    artifact = c.post(
        f"/v1/projects/{project}/model-artifacts",
        {
            "document": document,
            "signatures": [signing.sign(parse_document(document).digest(), PUBLISHER)],
        },
    ).json()["metadata"]["id"]
    profile = c.post(
        f"/v1/projects/{project}/execution-profiles",
        {"document": json.loads(files.execution_profile.read_text())},
    ).json()["metadata"]["id"]
    robot = c.post(
        f"/v1/projects/{project}/robots",
        {
            "spec": {
                "name": "robot-0",
                "display_name": "Robot 0",
                "site_id": site,
                "robot_profile_id": robot_profile,
            }
        },
    ).json()["metadata"]["id"]
    deployment = c.post(
        f"/v1/projects/{project}/deployments",
        {
            "spec": {
                "name": "pilot",
                "display_name": "Pilot",
                "site_id": site,
                "robot_ids": [robot],
                "rollout": {"strategy": "all_at_task_boundary"},
                "capture": {"mode": "all"},
                "release_gates": {"evaluations": ["replay"]},
            }
        },
    ).json()["metadata"]["id"]
    base = c.post(
        f"/v1/deployments/{deployment}/revisions",
        {
            "spec": {
                "model_artifact_id": artifact,
                "execution_profile_id": profile,
                "robot_profile_id": robot_profile,
            }
        },
    ).json()["metadata"]["id"]
    # The robot runs the base (activation itself is tested in test_site_agent_e2e).
    db(
        settings,
        update(Deployment)
        .where(Deployment.id == deployment)
        .values(observed={"phase": "Active", "active_revision_ids": {robot: base}}),
    )

    # --- the site: an agent with the base model's weights and recorded episodes ---------------
    agent = AgentRuntime(
        AgentSettings(
            state_dir=tmp_path / "agent",
            development=True,
            control_plane_insecure=True,
            gateway_listen="127.0.0.1:0",
            gateway_status_listen=None,
            status_interval_s=0.3,
            reconnect_max_backoff_s=0.5,
            gateway_backend="simulated",
            trusted_publisher_keys=[signing.raw_public_key(PUBLISHER).hex()],
            worker_sandbox="process",
            capture_allowed="all",
            job_device="cpu",
        ),
        gateway=InProcessGateway(),
        command_runner=lambda args: None,
    )
    token = c.post(f"/v1/sites/{site}/enrollment-tokens", {}).json()["secret"]
    site_loop.run(agent.enroll(endpoint, token))
    site_loop.run(agent.start())
    try:
        improve(c, agent, deployment, base, document, files, tmp_path, settings)
    finally:
        site_loop.run(agent.stop())


def improve(
    c: Any,
    agent: AgentRuntime,
    deployment: str,
    base: str,
    document: dict[str, Any],
    files: Any,
    tmp_path: Path,
    settings: Any,
) -> None:
    core = agent.core
    assert core is not None
    wait_for(lambda: core.state.manifest_generation > 0, what="the site's manifest")
    assert files.checkpoint is not None
    core.artifacts.import_directory(files.checkpoint)
    for i, outcome in enumerate(["success", "failure", "success", "success"]):
        episode = synthetic_episode(
            tmp_path / "recorded",
            files.binding,
            robot_id="robot-0",
            revision_id=base,
            steps=40,
            image_stride=2,
            seed=i,
            outcome=outcome,
        )
        upload = core.capture.store.begin(canonical(episode.manifest), robot_id="robot-0")
        for name in sorted(episode.manifest["files"]):
            upload.write(name, (episode.directory / name).read_bytes())
        assert upload.commit()

    def finished(operation_id: str) -> dict[str, Any] | None:
        operation: dict[str, Any] = c.get(f"/v1/operations/{operation_id}").json()
        return operation if operation["state"] in ("Succeeded", "Failed", "Cancelled") else None

    # --- fine-tune at the site ------------------------------------------------------------------
    started = c.post(
        f"/v1/deployments/{deployment}/training-jobs",
        {
            "spec": {
                "base_revision_id": base,
                "episodes": {"outcomes": ["success"]},
                "recipe": {"steps": 2, "batch_size": 2, "num_workers": 0, "log_every": 1},
                "artifact_version": "site-ft-1",
            }
        },
    )
    assert started.status_code == 202, started.text
    operation = wait_for(
        lambda: finished(started.json()["metadata"]["id"]), timeout=600, what="training"
    )
    assert operation["state"] == "Succeeded", operation
    job = c.get(f"/v1/training-jobs/{started.json()['target']}").json()["status"]
    assert job["phase"] == "Succeeded"
    assert job["dataset_episodes"] == 3  # the three successes
    assert job["metrics"]["steps"] == 2
    trained = c.get(f"/v1/model-artifacts/{job['artifact_id']}").json()
    lineage = trained["document"]["spec"]["lineage"]
    assert lineage["base_artifact"] == parse_document(document).digest()
    assert lineage["datasets"][0]["digest"] == job["dataset_digest"]
    assert trained["document"]["metadata"]["version"] == "site-ft-1"
    candidate = job["candidate_revision_id"]
    assert c.get(f"/v1/revisions/{candidate}").json()["status"]["compatibility"]["compatible"]
    # What left the site: the documents, counts and the loss, never an episode or a weight.
    assert set(operation["result"]) == {
        "training",
        "artifact",
        "artifact_digest",
        "artifact_signature",
        "execution_profile",
        "dataset_digest",
        "dataset_episodes",
    }
    assert set(operation["result"]["training"]["dataset"]) == {
        "episodes",
        "frames",
        "fps",
        "skipped",
        "skipped_reasons",
    }
    assert all(set(w) <= {"path", "digest", "size_bytes"} for w in lineage_weights(trained))
    wait_for(
        lambda: any(
            r["id"] == candidate
            for d in core.manifest().get("deployments", [])
            for r in d.get("revisions", [])
        ),
        what="the candidate in the site's manifest",
    )

    # --- the gate: evaluate against what the robot runs, on its recorded episodes ---------------
    db(settings, update(Revision).where(Revision.id == candidate).values(phase="Prepared"))
    activations = f"/v1/deployments/{deployment}/activation-requests"
    blocked = c.post(activations, {"revision_id": candidate})
    assert blocked.status_code == 409
    assert blocked.json()["reason"] == "API_RELEASE_GATE_UNMET"
    started = c.post(
        f"/v1/deployments/{deployment}/evaluations",
        {"spec": {"candidate_revision_id": candidate, "suite": {"kind": "replay", "stride": 8}}},
    )
    assert started.status_code == 202, started.text
    operation = wait_for(
        lambda: finished(started.json()["metadata"]["id"]), timeout=600, what="evaluation"
    )
    assert operation["state"] == "Succeeded", operation
    evaluation = c.get(f"/v1/evaluations/{started.json()['target']}").json()
    assert evaluation["spec"]["baseline_revision_id"] == base
    report = evaluation["status"]["report"]
    assert report["episodes"] == 4
    assert report["candidate"]["success"]["samples"] > 0
    # Two steps of fine-tuning hardly move a model: it reproduces what worked as its base did.
    assert evaluation["status"]["verdict"] == "non_inferior", report
    activated = c.post(activations, {"revision_id": candidate})
    assert activated.status_code == 202, activated.text
