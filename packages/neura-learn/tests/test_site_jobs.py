"""Evaluation and training at the site, as the agent runs them: the control plane's command, the
revision verified, episodes snapshotted from the site's store, ``neura-learn`` run as a
sandboxed child process, and the result reported, all of it allowed out by the published
schema. A training job's artifact is signed by the site, traceable to its base and data, and
its weights are in the site's artifact store."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("lerobot.datasets.lerobot_dataset", exc_type=ImportError)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from neura.v1 import site_pb2
from neura_agent import trust
from neura_agent.core import AgentCore
from neura_agent.identity import Identity
from neura_agent.outbound import filter_result
from neura_agent.settings import AgentSettings
from neura_bench.deployments import synthetic
from neura_contracts import ExecutionProfile, ModelArtifact, parse_document, signing
from neura_contracts.canonical import canonical_json, content_digest
from neura_contracts.site_protocol import MANIFEST_API_VERSION
from neura_runtime.capture.episode import canonical
from neura_runtime.capture.testing import synthetic_episode

SITE = "sit_01J00000000000000000000001"
REVISION = "rev_01J00000000000000000000001"


class FakeGateway:
    running = False

    async def start(self, config_path: Path) -> None:
        self.running = True

    async def reload(self) -> None:
        pass

    async def stop(self) -> None:
        self.running = False

    async def status(self) -> dict[str, Any] | None:
        return None


def site(tmp_path: Path) -> tuple[AgentCore, Any]:
    files = synthetic(tmp_path / "model", model="act", size="small", image=(96, 128))
    publisher = Ed25519PrivateKey.generate()
    raw = {
        "model_artifact": json.loads(files.artifact.read_text()),
        "robot_profile": json.loads(files.robot_profile.read_text()),
        "execution_profile": json.loads(files.execution_profile.read_text()),
    }
    documents: dict[str, dict[str, Any]] = {
        role: {"id": f"doc_{role}", "digest": parse_document(doc).digest(), "document": doc}
        for role, doc in raw.items()
    }
    artifact = documents["model_artifact"]
    artifact["signatures"] = [signing.sign(artifact["digest"], publisher)]
    revision = {
        "id": REVISION,
        "revision_digest": content_digest({r: e["digest"] for r, e in documents.items()}),
        "documents": documents,
    }
    manifest = {
        "api_version": MANIFEST_API_VERSION,
        "site_id": SITE,
        "project_id": "prj_01J0000000000000000000000P",
        "generation": 1,
        "offline_authorization_hours": 1,
        "robots": [{"id": "rbt_01J0000000000000000000000R", "name": "robot-0"}],
        "deployments": [
            {
                "id": "dep_1",
                "robot_ids": ["rbt_01J0000000000000000000000R"],
                "revisions": [revision],
                "capture": {"mode": "all"},
            }
        ],
    }
    settings = AgentSettings(
        state_dir=tmp_path / "agent",
        development=True,
        gateway_backend="simulated",
        trusted_publisher_keys=[signing.raw_public_key(publisher).hex()],
        worker_sandbox="process",
        capture_allowed="all",
        job_device="cpu",
    )
    identity = Identity(
        server_id="srv_01J0000000000000000000000V",
        site_id=SITE,
        project_id="prj_01J0000000000000000000000P",
        endpoint="127.0.0.1:1",
        manifest_public_key_hex="00" * 32,
        manifest_key_id="test",
    )
    core = AgentCore(settings, identity, FakeGateway())
    core.state.manifest_payload = canonical_json(manifest).decode()
    assert files.checkpoint is not None
    core.artifacts.import_directory(files.checkpoint)  # the base model's weights at the site
    store = core.capture.store
    for i, outcome in enumerate(["success", "failure", "success"]):
        episode = synthetic_episode(
            tmp_path / "recorded",
            files.binding,
            robot_id="robot-0",
            revision_id=REVISION,
            steps=40,
            image_stride=2,
            seed=i,
            outcome=outcome,
        )
        upload = store.begin(canonical(episode.manifest), robot_id="robot-0")
        for name in sorted(episode.manifest["files"]):
            upload.write(name, (episode.directory / name).read_bytes())
        assert upload.commit()
    return core, files


async def finish(core: AgentCore, command: site_pb2.Command) -> site_pb2.OperationReport:
    await core.on_command(command)
    for _ in range(1200):
        if not core.jobs.running():
            break
        await asyncio.sleep(0.1)
    reports = []
    while not core.outbox.empty():
        message = core.outbox.get_nowait()
        if message.WhichOneof("body") == "operation":
            reports.append(message.operation)
    assert any(r.state == "Running" for r in reports)
    return reports[-1]


async def test_the_site_evaluates_and_fine_tunes_on_its_own_episodes(tmp_path: Path) -> None:
    core, files = site(tmp_path)
    evaluation = await finish(
        core,
        site_pb2.Command(
            command_id="cmd_1",
            operation_id="op_1",
            evaluate=site_pb2.RunEvaluation(
                evaluation_id="evl_1",
                candidate_revision_id=REVISION,
                baseline_revision_id=REVISION,
                suite_json=json.dumps({"kind": "replay", "stride": 8, "filter": {}}),
            ),
        ),
    )
    assert evaluation.state == "Succeeded", evaluation.message
    result = json.loads(evaluation.result_json)
    report = result["evaluation"]
    assert report["kind"] == "replay"
    assert report["verdict"] == "non_inferior"  # the same model as its own baseline
    assert report["episodes"] == 3
    assert report["dataset_digest"].startswith("sha256:")
    _, _, unknown = filter_result(result, "standard")
    assert unknown == []  # every key is in the published schema
    datasets = core.capture.store.datasets()
    assert all(d["released"] for d in datasets)  # an evaluation keeps no episodes from retention

    training = await finish(
        core,
        site_pb2.Command(
            command_id="cmd_2",
            operation_id="op_2",
            train=site_pb2.RunTraining(
                training_job_id="trn_1",
                base_revision_id=REVISION,
                dataset_filter_json=json.dumps({"outcomes": ["success"]}),
                recipe_json=json.dumps(
                    {"steps": 2, "batch_size": 2, "num_workers": 0, "log_every": 1}
                ),
                artifact_name="act-so101-site",
                artifact_version="ft-1",
            ),
        ),
    )
    assert training.state == "Succeeded", training.message
    result = json.loads(training.result_json)
    _, _, unknown = filter_result(result, "standard")
    assert unknown == []
    artifact = parse_document(result["artifact"])
    assert isinstance(artifact, ModelArtifact)
    assert artifact.digest() == result["artifact_digest"]
    assert (artifact.metadata.name, artifact.metadata.version) == ("act-so101-site", "ft-1")
    lineage = artifact.spec.lineage
    assert lineage is not None
    assert lineage.base_artifact == files.binding.artifact_digest
    assert lineage.datasets[0].digest == result["dataset_digest"]
    assert lineage.datasets[0].episodes == result["dataset_episodes"] == 2  # the two successes
    assert lineage.training.steps == 2
    # Served as its base: the base's execution profile, bound to the new artifact.
    profile = parse_document(result["execution_profile"])
    assert isinstance(profile, ExecutionProfile)
    assert profile.spec.binding.model_artifact == artifact.digest()
    base_profile = files.binding.profile
    assert profile.spec.chunk == base_profile.spec.chunk
    # Signed by the site's training key, which this site now trusts.
    signature = result["artifact_signature"]
    assert signing.verify(artifact.digest(), signature)
    assert signing.trusted_signer(
        artifact.digest(), [signature], trust.trusted_publishers(core.settings)
    )
    # The trained weights are in the site's artifact store, verified against the artifact.
    checkout = core.artifacts.checkout(artifact)
    assert (checkout / "model.safetensors").is_file()
    assert artifact.spec.weights != files.binding.artifact.spec.weights
    # The training dataset is kept: the lineage names it.
    kept = [d for d in core.capture.store.datasets() if not d["released"]]
    assert [d["digest"] for d in kept] == [result["dataset_digest"]]


async def test_jobs_never_take_a_serving_gpu(tmp_path: Path) -> None:
    core, _ = site(tmp_path)
    core._cuda_indexes = lambda: [0]  # one GPU, and a worker serves on it
    core.state.prepared[REVISION] = "sha256:" + "0" * 64
    core.state.placements[REVISION] = {"device": "cuda:0"}
    core.settings = core.jobs.settings = core.settings.model_copy(update={"job_device": "auto"})
    report = await finish(
        core,
        site_pb2.Command(
            command_id="cmd_1",
            operation_id="op_1",
            evaluate=site_pb2.RunEvaluation(
                evaluation_id="evl_1",
                candidate_revision_id=REVISION,
                suite_json=json.dumps({"kind": "replay"}),
            ),
        ),
    )
    assert report.state == "Failed"
    assert report.reason.code == "AGENT_JOB_DEVICE_BUSY"
