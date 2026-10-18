"""Evaluation and fine-tuning on the site's episodes, with real LeRobot models (synthetic
weights): replay against a candidate, a fine-tune that produces a loadable checkpoint, the
simulated scenes, and the job entry point the agent runs."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

pytest.importorskip("lerobot.datasets.lerobot_dataset", exc_type=ImportError)

from neura_learn import jobs
from neura_learn.evaluate import (
    ReplaySuite,
    action_ranges,
    adapter_predict,
    evaluate_replay,
    replay_samples,
)
from neura_learn.train import Recipe, base_statistics, fine_tune

from neura_bench.deployments import DeploymentFiles, synthetic
from neura_runtime.capture.episode import Episode
from neura_runtime.capture.testing import synthetic_episode


@pytest.fixture(scope="module")
def model(tmp_path_factory: pytest.TempPathFactory) -> DeploymentFiles:
    return synthetic(tmp_path_factory.mktemp("model"), model="act", size="small", image=(96, 128))


def episodes(directory: Path, files: DeploymentFiles, count: int = 3) -> list[Episode]:
    outcomes = ["success", "failure", "success", "unknown"]
    return [
        synthetic_episode(
            directory, files.binding, steps=40, image_stride=2, seed=i, outcome=outcomes[i % 4]
        )
        for i in range(count)
    ]


def test_replay_measures_how_far_a_model_is_from_what_robots_did(
    tmp_path: Path, model: DeploymentFiles
) -> None:
    recorded = episodes(tmp_path / "eps", model)
    suite = ReplaySuite(stride=4)
    samples = replay_samples(recorded, model.binding, suite)
    assert samples
    assert {outcome for _, outcome, _, _ in samples} == {"success", "failure"}

    def key(inputs: Any) -> bytes:
        return b"".join(v.tobytes() for v in inputs.state.values())

    by_state = {key(inputs): executed for _, _, inputs, executed in samples}

    def perfect(batch: Sequence[Any]) -> list[NDArray[np.float32]]:
        """Predicts exactly what the robot did next."""
        return [by_state[key(inputs)] for inputs in batch]

    ranges = action_ranges(model.binding)

    def off(batch: Sequence[Any]) -> list[NDArray[np.float32]]:
        """10% of each action's range away from what the robot did."""
        return [(prediction + 0.1 * ranges).astype(np.float32) for prediction in perfect(batch)]

    report = evaluate_replay(
        recorded, off, model.binding, suite, baseline=perfect, baseline_binding=model.binding
    )
    assert report["baseline"]["success"]["mean"] == 0.0
    assert report["candidate"]["success"]["mean"] == pytest.approx(0.1)
    assert report["verdict"] == "inferior"
    report = evaluate_replay(recorded, perfect, model.binding, suite, baseline=perfect)
    assert report["verdict"] == "non_inferior"
    assert report["episodes"] == 3

    real = adapter_predict(model.binding, model.checkpoint)  # type: ignore[arg-type]
    report = evaluate_replay(recorded, real, model.binding, suite)
    assert report["candidate"]["all"]["samples"] == len(samples)
    assert np.isfinite(report["candidate"]["all"]["mean"])


def test_a_fine_tune_produces_a_checkpoint_serving_can_load(
    tmp_path: Path, model: DeploymentFiles
) -> None:
    from neura_server.capture.export import export_lerobot

    recorded = episodes(tmp_path / "eps", model)
    root = tmp_path / "dataset"
    export_lerobot(recorded, model.binding, root)
    assert model.checkpoint is not None
    base = base_statistics(model.checkpoint)
    assert "observation.state" in base
    report = fine_tune(
        model.checkpoint,
        root,
        tmp_path / "out",
        Recipe(steps=3, batch_size=2, num_workers=0, log_every=1),
    )
    checkpoint = report.checkpoint
    assert (checkpoint / "model.safetensors").is_file()
    assert (checkpoint / "config.json").is_file()
    # The base model's normalization is kept (the recipe's default).
    assert (
        base_statistics(checkpoint)["observation.state"]["mean"]
        == base["observation.state"]["mean"]
    )
    assert report.losses
    assert report.losses[-1][0] == 3
    # Serving loads it with the base model's contracts and it produces chunks.
    predict = adapter_predict(model.binding, checkpoint)
    samples = replay_samples(recorded, model.binding, ReplaySuite(stride=8))
    (chunk,) = predict([samples[0][2]])
    assert chunk.shape == (model.binding.chunk_length, model.binding.action_dim)
    from safetensors.numpy import load_file

    before = load_file(model.checkpoint / "model.safetensors")
    after = load_file(checkpoint / "model.safetensors")
    assert any(not np.array_equal(before[k], after[k]) for k in before)  # it trained


def test_the_job_entry_point_runs_replay_and_training(
    tmp_path: Path, model: DeploymentFiles, capsys: pytest.CaptureFixture[str]
) -> None:
    eps = tmp_path / "eps"
    recorded = episodes(eps, model, count=2)
    (eps / "labels.json").write_text(
        json.dumps({recorded[1].episode_id: {"label": "success", "source": "operator"}})
    )
    assert jobs.load_episodes(eps)[1].manifest["outcome"]["label"] == "success"
    documents = {
        "model_artifact": json.loads(model.artifact.read_text()),
        "robot_profile": json.loads(model.robot_profile.read_text()),
        "execution_profile": json.loads(model.execution_profile.read_text()),
    }
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "kind": "evaluate",
                "candidate": {"documents": documents},
                "baseline": {"documents": documents},
                "suite": {"kind": "replay", "stride": 8},
            }
        )
    )
    checkpoint = str(model.checkpoint)
    out = tmp_path / "evaluation"
    assert (
        jobs.main(
            [
                "evaluate",
                "--spec",
                str(spec),
                "--candidate",
                checkpoint,
                "--baseline",
                checkpoint,
                "--episodes",
                str(eps),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads((out / "report.json").read_text())
    assert report["kind"] == "replay"
    assert report["verdict"] == "non_inferior"  # the same model as its own baseline
    spec.write_text(
        json.dumps(
            {
                "kind": "train",
                "base": {"documents": documents},
                "recipe": {"steps": 2, "batch_size": 2, "num_workers": 0, "log_every": 1},
            }
        )
    )
    out = tmp_path / "training"
    code = jobs.main(
        [
            "train",
            "--spec",
            str(spec),
            "--base",
            checkpoint,
            "--episodes",
            str(eps),
            "--out",
            str(out),
        ]
    )
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    assert code == 0, lines[-3:]
    report = json.loads((out / "report.json").read_text())
    assert report["dataset"]["episodes"] == 2
    assert report["training"]["steps"] == 2
    assert (out / "checkpoint" / "model.safetensors").is_file()
    assert lines[-1]["event"] == "done"
    assert any(line["event"] == "progress" for line in lines)


@pytest.mark.skipif(
    os.environ.get("MUJOCO_GL") is None
    and not Path("/usr/lib/x86_64-linux-gnu/libEGL.so.1").exists(),
    reason="no offscreen OpenGL for the simulated scenes",
)
def test_simulation_compares_two_models_on_the_same_scenes(tmp_path: Path) -> None:
    os.environ.setdefault("MUJOCO_GL", "egl")
    pytest.importorskip("mujoco")
    from neura_learn.evaluate import SimulationSuite, evaluate_simulation

    from neura_robots.evidence.synthetic import write_checkpoint

    candidate = write_checkpoint(tmp_path / "candidate", size="tiny", seed=1)
    baseline = write_checkpoint(tmp_path / "baseline", size="tiny", seed=2)
    suite = SimulationSuite(episodes=3, max_steps=5, margin=0.1)
    report = evaluate_simulation(candidate, baseline, suite)
    assert report["kind"] == "simulation"
    assert [s["seed"] for s in report["scenes"]] == [1000, 1001, 1002]
    assert report["comparison"]["scenes"] == 3
    # Random weights in five steps solve nothing: no evidence either way.
    assert report["verdict"] == "inconclusive"
