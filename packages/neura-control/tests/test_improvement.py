"""Evaluations, training jobs and release gates through the control plane (design §28.3, §28.4):
what goes to the site, what the site may send back, and what the control plane makes of it.

The site's side (``neura-learn`` in the worker sandbox) is tested in packages/neura-learn; here
the site is a stream that answers the commands as the agent does."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import update
from test_site_control import Harness, connect, hello, kinds

from conftest import AppFactory, contract
from neura.v1 import site_pb2
from neura_agent.identity import connection_token
from neura_contracts import parse_document, signing
from neura_control.db.models import Deployment, Revision

DATASET = "sha256:" + "d" * 64


class Site:
    """The control plane, a site with two robots in one deployment, and a base revision the
    robots run."""

    def __init__(self, harness: Harness, *, gates: list[str] | None = None) -> None:
        self.harness = harness
        self.c = c = harness.client
        self.enrolled, self.key = harness.enrolled()
        for name in ("robot-0", "robot-1"):
            harness.add_robot(name)
        self.robots = {
            r["spec"]["name"]: r["metadata"]["id"]
            for r in c.get(f"/v1/projects/{harness.project}/robots").json()["items"]
        }
        created = c.post(
            f"/v1/projects/{harness.project}/deployments",
            {
                "spec": {
                    "name": "cell",
                    "display_name": "Cell",
                    "site_id": harness.site,
                    "robot_ids": sorted(self.robots.values()),
                    "rollout": {"strategy": "all_at_task_boundary"},
                    "capture": {"mode": "all"},
                    "release_gates": {"evaluations": gates or []},
                }
            },
        )
        assert created.status_code == 201, created.text
        self.deployment = created.json()["metadata"]["id"]
        self.artifact_document = contract("act-so101.artifact.yaml")
        self.profile_document = contract("act-so101-30hz.profile.yaml")
        artifact = c.post(
            f"/v1/projects/{harness.project}/model-artifacts", {"document": self.artifact_document}
        ).json()["metadata"]["id"]
        profile = c.post(
            f"/v1/projects/{harness.project}/execution-profiles",
            {"document": self.profile_document},
        ).json()["metadata"]["id"]
        self.base = self.revision(artifact, profile)
        self.run(self.base)

    def revision(self, artifact: str, profile: str) -> str:
        response = self.c.post(
            f"/v1/deployments/{self.deployment}/revisions",
            {
                "spec": {
                    "model_artifact_id": artifact,
                    "execution_profile_id": profile,
                    "robot_profile_id": self.harness.robot_profile,
                }
            },
        )
        assert response.status_code == 201, response.text
        revision_id: str = response.json()["metadata"]["id"]
        return revision_id

    def run(self, revision_id: str) -> None:
        """As if the site had activated the revision on every robot."""
        active = dict.fromkeys(self.robots.values(), revision_id)
        self.harness.db(
            update(Deployment)
            .where(Deployment.id == self.deployment)
            .values(observed={"phase": "Active", "active_revision_ids": active})
        )

    def prepared(self, revision_id: str) -> None:
        self.harness.db(update(Revision).where(Revision.id == revision_id).values(phase="Prepared"))

    def exchange(
        self, reports: list[site_pb2.OperationReport] | None = None
    ) -> list[site_pb2.ControlPlaneMessage]:
        """Connect as the site's agent, send ``reports``, and return what the control plane sent
        once it has applied them (it answers the status challenge sent after them)."""
        outgoing = [hello(self.harness.site)]
        outgoing += [site_pb2.AgentMessage(operation=r) for r in reports or []]
        outgoing.append(
            site_pb2.AgentMessage(
                status=site_pb2.SiteStatus(
                    offline_authorization_remaining_s=7200, contact_nonce=bytes(16)
                )
            )
        )
        return asyncio.run(
            connect(
                self.harness.endpoint,
                connection_token(self.key, self.enrolled.server_id),
                outgoing,
                until=lambda r: {"desired_state", "contact"} <= set(kinds(r)),
            )
        )

    def command(self, operation_id: str) -> site_pb2.Command:
        messages = asyncio.run(
            connect(
                self.harness.endpoint,
                connection_token(self.key, self.enrolled.server_id),
                [hello(self.harness.site)],
                until=lambda r: any(
                    m.WhichOneof("body") == "command" and m.command.operation_id == operation_id
                    for m in r
                ),
            )
        )
        return next(
            m.command
            for m in messages
            if m.WhichOneof("body") == "command" and m.command.operation_id == operation_id
        )

    def manifest(self) -> dict[str, Any]:
        messages = self.exchange()
        desired = next(m.desired_state for m in messages if m.WhichOneof("body") == "desired_state")
        manifest: dict[str, Any] = json.loads(desired.manifest)
        return manifest


def report(
    command: site_pb2.Command, state: str, result: dict[str, Any] | None = None, message: str = ""
) -> site_pb2.OperationReport:
    return site_pb2.OperationReport(
        command_id=command.command_id,
        operation_id=command.operation_id,
        state=state,
        message=message,
        result_json=json.dumps(result).encode() if result is not None else b"",
    )


def trained(
    site: Site, command: site_pb2.Command, key: Ed25519PrivateKey, *, episodes: int = 12
) -> dict[str, Any]:
    """What the site's agent reports for a fine-tune: the base's artifact with new weights and
    its lineage, signed by the site, and the base's execution profile bound to it."""
    train = command.train
    base_digest = parse_document(site.artifact_document).digest()
    artifact = copy.deepcopy(parse_document(site.artifact_document).canonical_data())
    artifact["metadata"] = {
        **artifact["metadata"],  # type: ignore[dict-item]
        "name": train.artifact_name,
        "version": train.artifact_version,
    }
    spec: dict[str, Any] = artifact["spec"]  # type: ignore[assignment]
    spec["weights"] = [
        {"path": "model.safetensors", "digest": "sha256:" + "e" * 64, "size_bytes": 2048}
    ]
    spec.pop("evidence", None)
    spec["lineage"] = {
        "base_artifact": base_digest,
        "datasets": [{"digest": DATASET, "episodes": episodes, "site_id": site.harness.site}],
        "training": {
            "recipe": {"steps": 2, "batch_size": 2},
            "steps": 2,
            "job_id": train.training_job_id,
            "trainer": "neura-learn 0.1.0; lerobot 0.4.0",
        },
    }
    digest = parse_document(artifact).digest()
    profile = copy.deepcopy(parse_document(site.profile_document).canonical_data())
    profile["spec"]["binding"]["model_artifact"] = digest  # type: ignore[index]
    return {
        "training": {
            "recipe": {"steps": 2, "batch_size": 2},
            "dataset": {"episodes": episodes, "frames": 480, "fps": 30, "skipped": 0},
            "training": {"steps": 2, "seconds": 3.5, "loss_first": 1.2, "loss_last": 0.9},
        },
        "artifact": artifact,
        "artifact_digest": digest,
        "artifact_signature": signing.sign(digest, key),
        "execution_profile": profile,
        "dataset_digest": DATASET,
        "dataset_episodes": episodes,
    }


@pytest.fixture
def site(make_api: AppFactory, settings: Any) -> Site:
    return Site(Harness(make_api, settings), gates=["replay"])


def test_a_site_fine_tune_becomes_a_candidate_revision_that_must_pass_its_gate(site: Site) -> None:
    c = site.c
    jobs = f"/v1/deployments/{site.deployment}/training-jobs"
    refused = c.post(
        jobs, {"spec": {"base_revision_id": site.base, "episodes": {"robot_names": ["x"]}}}
    )
    assert refused.status_code == 422
    assert "not in this deployment" in refused.json()["detail"]
    accepted = c.post(
        jobs,
        {
            "spec": {
                "base_revision_id": site.base,
                "episodes": {"outcomes": ["success"], "since": "2026-10-01T00:00:00Z"},
                "recipe": {"steps": 2, "batch_size": 2},
            }
        },
    )
    assert accepted.status_code == 202, accepted.text
    operation = accepted.json()
    assert operation["kind"] == "train"
    (job,) = c.get(jobs).json()["items"]
    assert job["metadata"]["id"] == operation["target"]
    assert job["status"]["phase"] == "Pending"
    assert job["spec"]["artifact_name"] == "act-so101"
    assert job["spec"]["artifact_version"].startswith("example-1-ft.")

    command = site.command(operation["metadata"]["id"])
    assert command.WhichOneof("kind") == "train"
    train = command.train
    assert train.training_job_id == job["metadata"]["id"]
    assert train.base_revision_id == site.base
    assert (train.artifact_name, train.artifact_version) == (
        job["spec"]["artifact_name"],
        job["spec"]["artifact_version"],
    )
    # The site's filter: robots by the name the site knows them by, only this deployment's.
    assert json.loads(train.dataset_filter_json) == {
        "robot_ids": ["robot-0", "robot-1"],
        "revision_ids": [site.base],
        "outcomes": ["success"],
        "end_reasons": [],
        "complete_only": True,
        "since": "2026-10-01T00:00:00.000+00:00",
    }
    assert json.loads(train.recipe_json)["steps"] == 2

    site_key = Ed25519PrivateKey.generate()
    result = trained(site, command, site_key)
    site.exchange([report(command, "Running", message="step 1: loss 1.2000")])
    running = c.get(f"/v1/training-jobs/{job['metadata']['id']}").json()
    assert (running["status"]["phase"], running["status"]["message"]) == (
        "Running",
        "step 1: loss 1.2000",
    )
    site.exchange([report(command, "Succeeded", result)])
    done = c.get(f"/v1/training-jobs/{job['metadata']['id']}").json()["status"]
    assert done["phase"] == "Succeeded", done
    assert done["dataset_digest"] == DATASET
    assert done["dataset_episodes"] == 12
    assert done["metrics"]["loss_last"] == 0.9
    assert done["metrics"]["frames"] == 480

    # The new artifact, signed by the site, with its lineage; the base's profile bound to it.
    artifact = c.get(f"/v1/model-artifacts/{done['artifact_id']}").json()
    assert artifact["digest"] == result["artifact_digest"]
    assert artifact["document"]["spec"]["lineage"]["base_artifact"] == (
        parse_document(site.artifact_document).digest()
    )
    assert [s["key_id"] for s in artifact["signatures"]] == [result["artifact_signature"]["key_id"]]
    profile = c.get(f"/v1/execution-profiles/{done['execution_profile_id']}").json()
    assert profile["document"]["spec"]["binding"]["model_artifact"] == artifact["digest"]
    candidate = c.get(f"/v1/revisions/{done['candidate_revision_id']}").json()
    assert candidate["status"]["phase"] == "Created"
    assert candidate["status"]["compatibility"]["compatible"] is True
    assert job["metadata"]["id"] in candidate["spec"]["notes"]
    candidate_id = candidate["metadata"]["id"]
    # The site receives the candidate in its manifest, to prepare and evaluate it.
    (deployment,) = site.manifest()["deployments"]
    assert candidate_id in [r["id"] for r in deployment["revisions"]]

    # The deployment's gate: no activation without a passing replay evaluation.
    site.prepared(candidate_id)
    activations = f"/v1/deployments/{site.deployment}/activation-requests"
    blocked = c.post(activations, {"revision_id": candidate_id})
    assert blocked.status_code == 409
    assert blocked.json()["reason"] == "API_RELEASE_GATE_UNMET"
    assert f"replay evaluation of {candidate_id} against {site.base}" in blocked.json()["detail"]

    evaluations = f"/v1/deployments/{site.deployment}/evaluations"
    created = c.post(
        evaluations,
        {"spec": {"candidate_revision_id": candidate_id, "suite": {"kind": "replay", "stride": 4}}},
    )
    assert created.status_code == 202, created.text
    (evaluation,) = c.get(evaluations).json()["items"]
    assert evaluation["spec"]["baseline_revision_id"] == site.base  # what the robots run
    command = site.command(created.json()["metadata"]["id"])
    assert command.WhichOneof("kind") == "evaluate"
    suite = json.loads(command.evaluate.suite_json)
    assert suite["stride"] == 4
    assert suite["filter"]["robot_ids"] == ["robot-0", "robot-1"]
    assert set(suite["filter"]["revision_ids"]) == {site.base, candidate_id}
    assert command.evaluate.baseline_revision_id == site.base
    replay = {
        "kind": "replay",
        "episodes": 12,
        "samples": 96,
        "candidate": {"all": {"samples": 96, "mean": 0.02, "p95": 0.05}},
        "baseline": {"all": {"samples": 96, "mean": 0.02, "p95": 0.05}},
        "verdict": "inferior",
        "dataset_digest": DATASET,
    }
    site.exchange([report(command, "Succeeded", {"evaluation": replay})])
    evaluated = c.get(f"/v1/evaluations/{evaluation['metadata']['id']}").json()["status"]
    assert (evaluated["phase"], evaluated["verdict"]) == ("Succeeded", "inferior")
    assert evaluated["report"]["samples"] == 96
    assert c.post(activations, {"revision_id": candidate_id}).status_code == 409  # inferior

    created = c.post(
        evaluations, {"spec": {"candidate_revision_id": candidate_id, "suite": {"kind": "replay"}}}
    )
    command = site.command(created.json()["metadata"]["id"])
    site.exchange(
        [report(command, "Succeeded", {"evaluation": {**replay, "verdict": "non_inferior"}})]
    )
    activated = c.post(activations, {"revision_id": candidate_id})
    assert activated.status_code == 202, activated.text
    # A rollback to what robots ran before needs no evidence.
    site.run(candidate_id)
    site.prepared(site.base)
    rollback = c.post(f"/v1/deployments/{site.deployment}/rollbacks", {"to_revision_id": site.base})
    assert rollback.status_code == 202, rollback.text


def _tamper(case: str, result: dict[str, Any], site: Site) -> dict[str, Any]:
    result = copy.deepcopy(result)
    if case == "wrong_base":
        result["artifact"]["spec"]["lineage"]["base_artifact"] = "sha256:" + "0" * 64
    elif case == "other_dataset":
        result["dataset_digest"] = "sha256:" + "1" * 64
    elif case == "changed_profile":
        result["execution_profile"]["spec"]["chunk"]["refill_threshold_actions"] += 1
    elif case == "changed_adapter":
        result["artifact"]["spec"]["adapter"]["version"] = "9.9.9"
    elif case == "bad_signature":
        result["artifact_signature"] = signing.sign(
            "sha256:" + "2" * 64, Ed25519PrivateKey.generate()
        )
        return result
    elif case == "missing_documents":
        del result["execution_profile"]
        return result
    digest = parse_document(result["artifact"]).digest()
    result["artifact_digest"] = digest
    result["execution_profile"]["spec"]["binding"]["model_artifact"] = digest
    return result


@pytest.mark.parametrize(
    "case",
    [
        "wrong_base",
        "other_dataset",
        "changed_profile",
        "changed_adapter",
        "bad_signature",
        "missing_documents",
    ],
)
def test_a_trained_model_that_does_not_verify_registers_nothing(site: Site, case: str) -> None:
    c = site.c
    accepted = c.post(
        f"/v1/deployments/{site.deployment}/training-jobs",
        {"spec": {"base_revision_id": site.base, "artifact_version": "2.0.0"}},
    )
    assert accepted.status_code == 202, accepted.text
    command = site.command(accepted.json()["metadata"]["id"])
    key = Ed25519PrivateKey.generate()
    result = trained(site, command, key)
    tampered = _tamper(case, result, site)
    site.exchange([report(command, "Succeeded", tampered)])
    operation = c.get(f"/v1/operations/{accepted.json()['metadata']['id']}").json()
    assert (operation["state"], operation["reason"]) == ("Failed", "TRAINING_RESULT_REJECTED")
    job = c.get(f"/v1/training-jobs/{accepted.json()['target']}").json()["status"]
    assert (job["phase"], job["reason"]) == ("Failed", "TRAINING_RESULT_REJECTED")
    assert job["candidate_revision_id"] is None
    assert job["artifact_id"] is None
    revisions = c.get(f"/v1/deployments/{site.deployment}/revisions").json()["items"]
    assert [r["metadata"]["id"] for r in revisions] == [site.base]
    artifacts = c.get(f"/v1/projects/{site.harness.project}/model-artifacts").json()["items"]
    assert len(artifacts) == 1


def test_one_name_and_version_names_one_model(site: Site) -> None:
    jobs = f"/v1/deployments/{site.deployment}/training-jobs"
    spec = {"base_revision_id": site.base, "artifact_version": "example-1"}
    taken = site.c.post(jobs, {"spec": spec})  # the base artifact is act-so101 example-1
    assert taken.status_code == 409
    spec["artifact_version"] = "example-2"
    assert site.c.post(jobs, {"spec": spec}).status_code == 202
    assert site.c.post(jobs, {"spec": spec}).status_code == 409  # a running job will take it


def test_evaluations_compare_with_a_revision_the_site_has(site: Site) -> None:
    c = site.c
    evaluations = f"/v1/deployments/{site.deployment}/evaluations"
    simulation = {"kind": "simulation", "episodes": 20}
    # The robots run the base: a simulation of the base has nothing else to compare with.
    alone = c.post(evaluations, {"spec": {"candidate_revision_id": site.base, "suite": simulation}})
    assert alone.status_code == 422
    assert "baseline_revision_id" in alone.json()["detail"]
    other = c.post(
        evaluations,
        {
            "spec": {
                "candidate_revision_id": "rev_01J00000000000000000000000",
                "suite": simulation,
            }
        },
    )
    assert other.status_code == 422
    assert (
        c.post(
            evaluations,
            {
                "spec": {
                    "candidate_revision_id": site.base,
                    "suite": {"kind": "simulation", "margin": 1},
                }
            },
        ).status_code
        == 422
    )
    accepted = c.post(
        evaluations,
        {
            "spec": {
                "candidate_revision_id": site.base,
                "baseline_revision_id": site.base,
                "suite": simulation,
            }
        },
    )
    assert accepted.status_code == 202, accepted.text
    command = site.command(accepted.json()["metadata"]["id"])
    suite = json.loads(command.evaluate.suite_json)
    assert suite == {
        "kind": "simulation",
        "scene": "aloha_transfer_cube",
        "episodes": 20,
        "first_seed": 1000,
        "max_steps": 400,
        "margin": 0.1,
    }
    # A report of another kind than asked is not recorded.
    site.exchange(
        [report(command, "Succeeded", {"evaluation": {"kind": "replay", "verdict": "superior"}})]
    )
    status = c.get(f"/v1/evaluations/{accepted.json()['target']}").json()["status"]
    assert (status["phase"], status["reason"], status["verdict"]) == (
        "Failed",
        "EVALUATION_REPORT_INVALID",
        None,
    )


def test_release_gates_are_part_of_the_deployment(site: Site) -> None:
    path = f"/v1/deployments/{site.deployment}"
    deployment = site.c.get(path).json()
    assert deployment["spec"]["release_gates"] == {"evaluations": ["replay"]}
    bad = site.c.patch(
        path,
        {"spec": {"release_gates": {"evaluations": ["vibes"]}}},
        version=deployment["metadata"]["version"],
    )
    assert bad.status_code == 422
    patched = site.c.patch(
        path,
        {"spec": {"release_gates": {"evaluations": ["simulation", "replay"]}}},
        version=deployment["metadata"]["version"],
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["spec"]["release_gates"]["evaluations"] == ["simulation", "replay"]
    # A first activation replaces nothing, and needs no evidence.
    site.harness.db(update(Deployment).where(Deployment.id == site.deployment).values(observed={}))
    site.prepared(site.base)
    activated = site.c.post(
        f"/v1/deployments/{site.deployment}/activation-requests", {"revision_id": site.base}
    )
    assert activated.status_code == 202, activated.text


def test_a_shadow_compares_with_what_the_robots_run(site: Site) -> None:
    c = site.c
    evaluations = f"/v1/deployments/{site.deployment}/evaluations"
    shadow = {"kind": "shadow", "duration_s": 30, "min_samples": 50}
    other = site.revision(
        c.get(f"/v1/revisions/{site.base}").json()["spec"]["model_artifact_id"],
        c.get(f"/v1/revisions/{site.base}").json()["spec"]["execution_profile_id"],
    )
    # Only a revision the robots run can be shadowed.
    refused = c.post(
        evaluations,
        {
            "spec": {
                "candidate_revision_id": site.base,
                "baseline_revision_id": other,
                "suite": shadow,
            }
        },
    )
    assert refused.status_code == 422
    assert "robots run" in refused.json()["detail"]
    bad = c.post(
        evaluations,
        {
            "spec": {
                "candidate_revision_id": other,
                "suite": {**shadow, "min_samples": 50, "max_samples": 10},
            }
        },
    )
    assert bad.status_code == 422
    accepted = c.post(evaluations, {"spec": {"candidate_revision_id": other, "suite": shadow}})
    assert accepted.status_code == 202, accepted.text
    command = site.command(accepted.json()["metadata"]["id"])
    assert command.evaluate.baseline_revision_id == site.base
    assert json.loads(command.evaluate.suite_json) == {
        "kind": "shadow",
        "duration_s": 30.0,
        "min_samples": 50,
        "every_nth": 1,
        "tolerance": 0.05,
    }
