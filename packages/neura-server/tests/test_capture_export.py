"""Exporting site episodes as a LeRobot dataset: model order and units, frames held between
recordings, and episodes that cannot train the model left out with the reason."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("lerobot.datasets.lerobot_dataset", exc_type=ImportError)

from lerobot.datasets.lerobot_dataset import LeRobotDataset

from neura_runtime.capture.testing import synthetic_episode
from neura_server.capture.export import export_lerobot, features, frames


def test_episodes_become_a_lerobot_dataset_in_model_units(
    tmp_path: Path, make_binding: Any
) -> None:
    binding = make_binding()
    first = synthetic_episode(tmp_path / "eps", binding, steps=6, image_stride=2, seed=1)
    second = synthetic_episode(tmp_path / "eps", binding, steps=4, seed=2)
    broken = synthetic_episode(tmp_path / "eps", binding, steps=4, seed=3, complete=False)
    report = export_lerobot([first, second, broken], binding, tmp_path / "dataset")
    assert report.episodes == [first.episode_id, second.episode_id]
    assert report.skipped == [
        {
            "episode_id": broken.episode_id,
            "reason": "incomplete: the robot dropped data while recording it",
        }
    ]
    assert report.frames == 10
    dataset = LeRobotDataset("neura/site", root=tmp_path / "dataset")
    assert dataset.num_episodes == 2
    assert dataset.num_frames == 10
    assert dataset.fps == round(1e6 / binding.control_period_us)
    item = dataset[0]
    expected = binding.action_transform.to_model(first.steps().action[:1])[0].astype(np.float32)
    np.testing.assert_allclose(item["action"].numpy(), expected, rtol=1e-6)
    layout = binding.state_layouts[0]
    names = first.manifest["state_names"]
    state = first.steps().state[0, [names.index(n) for n in layout.feature_names]]
    np.testing.assert_allclose(
        item[layout.input_name].numpy(), (state * layout.factors).astype(np.float32), rtol=1e-6
    )
    assert set(features(binding)) <= set(dataset.features)


def test_frames_are_held_between_recordings(tmp_path: Path, make_binding: Any) -> None:
    binding = make_binding()
    episode = synthetic_episode(tmp_path / "eps", binding, steps=5, image_stride=2)
    rows = frames(episode, binding)
    image = binding.image_layouts[0].input_name
    assert len(rows) == 5
    assert rows[1][image] is rows[0][image]  # period 1 had no frame: period 0's is held
    assert not np.array_equal(rows[2][image], rows[0][image])
    assert rows[0][image].shape == (480, 640, 3)


def test_episodes_from_another_robot_setup_are_left_out(tmp_path: Path, make_binding: Any) -> None:
    binding = make_binding()

    def slower(artifact: Any, robot: Any, profile: Any) -> None:
        for document in (profile["spec"], robot["spec"], artifact["spec"]["outputs"]):
            document["control_period_us"] = 20_000

    other = make_binding(edit=slower)
    episode = synthetic_episode(tmp_path / "eps", other, steps=3)
    with pytest.raises(ValueError, match="period"):
        export_lerobot([episode], binding, tmp_path / "dataset")
