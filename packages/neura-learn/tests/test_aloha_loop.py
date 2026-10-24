"""The improvement loop's data path on the simulated ALOHA cell (design §28.5): the cell's robot,
served by a site gateway, records its tasks, each labeled by the simulator; the recordings
fine-tune the served ACT model at the site (synthetic weights: the path, not the policy, is
under test); the new checkpoint is evaluated against its base on the same simulated scenes and
on the recordings.

A synthetic model solves no task, so this shows every stage works on the cell's real data (two
arms, fourteen joints, its cameras), not that the loop improves a policy: that needs the
published pretrained checkpoint, which the same stages take unchanged."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("lerobot.datasets.lerobot_dataset", exc_type=ImportError)
os.environ.setdefault("MUJOCO_GL", "egl")  # before MuJoCo is first imported
pytest.importorskip("mujoco")

from neura_learn import jobs

from neura_robots.aloha.cell import CellRobot
from neura_robots.aloha.contracts import Chunking, write
from neura_robots.evidence.synthetic import write_checkpoint
from neura_server.capture.service import CaptureServer
from neura_server.capture.store import EpisodeFilter, EpisodeStore
from neura_server.config import (
    CaptureConfig,
    DeploymentConfig,
    DeviceConfig,
    DeviceRegistry,
    GatewayConfig,
    ServingConfig,
    token_digest,
)
from neura_server.grpc_server import GatewayServer

pytestmark = pytest.mark.skipif(
    os.environ["MUJOCO_GL"] == "egl" and not Path("/usr/lib/x86_64-linux-gnu/libEGL.so.1").exists(),
    reason="no offscreen OpenGL for the simulated cell",
)
TOKEN = "token-aloha-0"
ROBOT = "aloha-0"
REVISION = "rev_aloha"


@pytest.fixture
async def cell_site(tmp_path: Path) -> AsyncIterator[tuple[str, EpisodeStore, Any]]:
    checkpoint = write_checkpoint(tmp_path / "base", size="tiny")
    deployment = write(
        tmp_path / "contracts",
        checkpoint,
        name="act-aloha",
        source="synthetic",
        chunking=Chunking(refill_at=30),
    )
    devices = [DeviceConfig(robot_id=ROBOT, token_sha256=token_digest(TOKEN))]
    store = EpisodeStore(tmp_path / "site-episodes")
    capture = CaptureServer(
        store,
        "127.0.0.1:0",
        registry=DeviceRegistry(devices),
        tls=None,
        allowed=lambda robot, revision: (robot, revision) == (ROBOT, REVISION),
    )
    await capture.start()
    gateway = GatewayServer(
        GatewayConfig(
            listen="127.0.0.1:0",
            status_listen=None,
            devices=devices,
            deployments=[
                DeploymentConfig(
                    revision_id=REVISION,
                    artifact=deployment.artifact,
                    robot_profile=deployment.robot_profile,
                    execution_profile=deployment.execution_profile,
                    robots=[ROBOT],
                    capture=CaptureConfig(
                        mode="all", endpoint=f"127.0.0.1:{capture.port}", image_stride=5
                    ),
                )
            ],
            assignments={ROBOT: REVISION},
            serving=ServingConfig(backend="simulated"),
        )
    )
    await gateway.start()
    yield f"127.0.0.1:{gateway.bound_port}", store, deployment
    await gateway.stop()
    await capture.stop()


def documents(deployment: Any) -> dict[str, Any]:
    return {
        "model_artifact": json.loads(deployment.artifact.read_text()),
        "robot_profile": json.loads(deployment.robot_profile.read_text()),
        "execution_profile": json.loads(deployment.execution_profile.read_text()),
    }


async def test_the_cells_recordings_fine_tune_and_evaluate_its_model(
    cell_site: tuple[str, EpisodeStore, Any], tmp_path: Path
) -> None:
    gateway, store, deployment = cell_site
    said: list[str] = []
    robot = CellRobot(
        ROBOT,
        TOKEN,  # type: ignore[arg-type]  # a development token, as the gateway is configured
        gateway=gateway,
        max_steps=120,  # longer than the actions a replay compares per prediction
        pause_s=0.2,
        say=said.append,
        capture_dir=tmp_path / "spool",
    )
    stop = threading.Event()
    thread = threading.Thread(target=robot.run, args=(stop,), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 90
        while store.summary()["episodes"] < 3:
            assert time.monotonic() < deadline, said[-10:]
            await asyncio.sleep(0.2)
    finally:
        stop.set()
        thread.join(timeout=30)
    records = store.find()
    assert {r["robot_id"] for r in records} == {ROBOT}
    # The simulator labeled each task: a synthetic model hands over no cube.
    labeled = [r for r in records if r["outcome_source"] != "rule"]
    assert labeled
    assert all(r["outcome"] == "failure" and "timeout" in r["outcome_note"] for r in labeled)
    episode = store.episode(records[0]["episode_id"])
    steps = episode.steps()
    assert steps.action.shape[1] == 14  # two arms
    assert (steps.frame >= 0).sum() > 0  # camera frames, every fifth period

    # The episodes as a site job sees them: a dataset snapshot, linked with its labels.
    dataset = store.create_dataset("aloha-loop", EpisodeFilter(complete_only=True))
    eps = tmp_path / "eps"
    eps.mkdir()
    labels = {}
    for episode_id in dataset["episode_ids"]:
        jobs_dir = eps / episode_id
        jobs_dir.mkdir()
        for path in store.path(episode_id).iterdir():
            (jobs_dir / path.name).write_bytes(path.read_bytes())
        record = store.get(episode_id)
        labels[episode_id] = {"label": record["outcome"], "source": record["outcome_source"]}
    (eps / "labels.json").write_text(json.dumps(labels))
    assert dataset["episodes"] >= 2

    spec = tmp_path / "train.json"
    spec.write_text(
        json.dumps(
            {
                "kind": "train",
                "base": {"documents": documents(deployment)},
                "recipe": {"steps": 2, "batch_size": 2, "num_workers": 0, "log_every": 1},
            }
        )
    )
    trained = tmp_path / "trained"
    assert (
        jobs.main(
            [
                "train",
                "--spec",
                str(spec),
                "--base",
                str(deployment.checkpoint),
                "--episodes",
                str(eps),
                "--out",
                str(trained),
            ]
        )
        == 0
    )
    training = json.loads((trained / "report.json").read_text())
    # Each episode is trained on or left out, and the report says why (on a loaded machine a
    # task can end too early to be used).
    used = training["dataset"]
    assert used["episodes"] + used["skipped"] == dataset["episodes"], used
    assert used["episodes"] >= 2, used
    assert training["dataset"]["frames"] > 0
    assert training["training"]["steps"] == 2
    candidate = trained / "checkpoint"
    assert (candidate / "model.safetensors").is_file()

    # The fine-tune against its base: on the same simulated scenes, and on the recordings.
    for suite in (
        {"kind": "simulation", "episodes": 2, "max_steps": 20, "margin": 0.1},
        {"kind": "replay", "stride": 4},
    ):
        spec.write_text(
            json.dumps(
                {
                    "kind": "evaluate",
                    "candidate": {"documents": documents(deployment)},
                    "baseline": {"documents": documents(deployment)},
                    "suite": suite,
                }
            )
        )
        out = tmp_path / f"evaluation-{suite['kind']}"
        arguments = [
            "evaluate",
            "--spec",
            str(spec),
            "--candidate",
            str(candidate),
            "--baseline",
            str(deployment.checkpoint),
            "--out",
            str(out),
        ]
        if suite["kind"] == "replay":
            arguments += ["--episodes", str(eps)]
        assert jobs.main(arguments) == 0
        report = json.loads((out / "report.json").read_text())
        assert report["kind"] == suite["kind"]
        assert report["verdict"] in ("superior", "non_inferior", "inconclusive", "inferior")
        if suite["kind"] == "simulation":
            assert report["comparison"]["scenes"] == 2
            assert report["candidate"]["successes"] == report["baseline"]["successes"] == 0
        else:
            assert report["episodes_offered"] == dataset["episodes"]
            assert 1 <= report["episodes"] <= dataset["episodes"], report
            assert report["samples"] > 0
