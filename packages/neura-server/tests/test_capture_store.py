"""The site's episode store: only intact episodes from the robot that recorded them are kept,
labels and datasets are recorded, and retention never deletes data a dataset still holds."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from neura_runtime.capture.episode import MANIFEST, Episode, canonical
from neura_runtime.capture.testing import synthetic_episode
from neura_server.capture.store import EpisodeFilter, EpisodeStore, StoreError


def upload(store: EpisodeStore, episode: Episode, *, robot_id: str = "rbt_1") -> bool:
    session = store.begin(canonical(episode.manifest), robot_id=robot_id)
    for name in sorted(episode.manifest["files"]):
        data = (episode.directory / name).read_bytes()
        for start in range(0, len(data), 4096):
            session.write(name, data[start : start + 4096])
    return session.commit()


@pytest.fixture
def store(tmp_path: Path) -> EpisodeStore:
    return EpisodeStore(tmp_path / "store")


@pytest.fixture
def episodes(tmp_path: Path, make_binding: Any) -> list[Episode]:
    binding = make_binding()
    robot = tmp_path / "robot"
    return [
        synthetic_episode(robot, binding, outcome="success", seed=1, image_stride=4),
        synthetic_episode(robot, binding, seed=2, end="takeover", image_stride=4),
        synthetic_episode(
            robot, binding, robot_id="rbt_2", revision_id="rev_2", seed=3, image_stride=4
        ),
    ]


def test_an_upload_is_stored_once_and_read_back(
    store: EpisodeStore, episodes: list[Episode]
) -> None:
    first = episodes[0]
    assert upload(store, first) is True
    assert upload(store, first) is False  # a retry after a lost acknowledgment
    record = store.get(first.episode_id)
    assert record["robot_id"] == "rbt_1"
    assert record["outcome"] == "success"
    assert record["steps"] == 8
    assert record["complete"] is True
    stored = store.episode(first.episode_id)
    assert (stored.steps().action == first.steps().action).all()
    assert store.summary()["episodes"] == 1


def test_a_robot_cannot_upload_another_robots_episode(
    store: EpisodeStore, episodes: list[Episode]
) -> None:
    with pytest.raises(StoreError) as caught:
        upload(store, episodes[0], robot_id="rbt_2")
    assert caught.value.code == "CAPTURE_ROBOT_MISMATCH"


def test_damaged_or_unexpected_files_are_refused(
    store: EpisodeStore, episodes: list[Episode]
) -> None:
    episode = episodes[0]
    session = store.begin(canonical(episode.manifest), robot_id="rbt_1")
    with pytest.raises(StoreError, match="not in the manifest"):
        session.write("../escape", b"x")
    session = store.begin(canonical(episode.manifest), robot_id="rbt_1")
    size = episode.manifest["files"]["steps.npz"]["bytes"]
    with pytest.raises(StoreError, match="larger"):
        session.write("steps.npz", b"\0" * (size + 1))
    session = store.begin(canonical(episode.manifest), robot_id="rbt_1")
    for name in sorted(episode.manifest["files"]):
        data = bytearray((episode.directory / name).read_bytes())
        if name == "steps.npz":
            data[-10] ^= 0xFF  # a flipped byte in transit
        session.write(name, bytes(data))
    with pytest.raises(StoreError, match="digest"):
        session.commit()
    assert not store.has(episode.episode_id)
    assert list((store.root / "incoming").iterdir()) == []
    forged = {**episode.manifest, "format": "other/9"}
    with pytest.raises(StoreError):
        store.begin(json.dumps(forged).encode(), robot_id="rbt_1")


def test_filters_labels_and_deletion(store: EpisodeStore, episodes: list[Episode]) -> None:
    for episode in episodes:
        upload(store, episode, robot_id=episode.manifest["robot_id"])
    assert len(store.find(EpisodeFilter(revision_ids=("rev_1",)))) == 2
    assert [e["end_reason"] for e in store.find(EpisodeFilter(end_reasons=("takeover",)))] == [
        "takeover"
    ]
    taken_over = episodes[1].episode_id
    record = store.label(taken_over, "failure", note="the cube slipped")
    assert (record["outcome"], record["outcome_source"]) == ("failure", "operator")
    assert store.episode(taken_over).manifest["outcome"]["note"] == "the cube slipped"
    with pytest.raises(StoreError):
        store.label(taken_over, "great")
    store.delete(episodes[2].episode_id)
    assert store.summary()["episodes"] == 2
    assert not store.path(episodes[2].episode_id).exists()


def test_datasets_pin_their_episodes_until_released(
    store: EpisodeStore, episodes: list[Episode]
) -> None:
    for episode in episodes:
        upload(store, episode, robot_id=episode.manifest["robot_id"])
    dataset = store.create_dataset("successes", EpisodeFilter(outcomes=("success",)))
    assert dataset["episode_ids"] == [episodes[0].episode_id]
    assert dataset["digest"].startswith("sha256:")
    again = store.create_dataset("successes", EpisodeFilter(outcomes=("success",)))
    assert again["digest"] == dataset["digest"]  # same data, same digest
    with pytest.raises(StoreError) as caught:
        store.delete(episodes[0].episode_id)
    assert caught.value.code == "CAPTURE_EPISODE_IN_DATASET"
    store.max_bytes = 0  # retention wants everything gone
    deleted = store.enforce_retention()
    assert set(deleted) == {episodes[1].episode_id, episodes[2].episode_id}
    assert store.has(episodes[0].episode_id)  # held by the datasets
    store.release_dataset(dataset["dataset_id"])
    store.release_dataset(again["dataset_id"])
    assert store.enforce_retention() == [episodes[0].episode_id]
    with pytest.raises(StoreError) as caught:
        store.dataset_episodes(dataset["dataset_id"])
    assert caught.value.code == "CAPTURE_DATASET_RELEASED"
    with pytest.raises(StoreError) as caught:
        store.create_dataset("nothing", EpisodeFilter(outcomes=("success",)))
    assert caught.value.code == "CAPTURE_DATASET_EMPTY"


def test_retention_by_age(store: EpisodeStore, episodes: list[Episode]) -> None:
    upload(store, episodes[0])
    store.max_age_days = 30
    assert store.enforce_retention(now=datetime(2026, 10, 18, tzinfo=UTC)) == []
    later = datetime(2026, 10, 17, tzinfo=UTC) + timedelta(days=31)
    assert store.enforce_retention(now=later) == [episodes[0].episode_id]


def test_a_restart_discards_partial_uploads(tmp_path: Path, episodes: list[Episode]) -> None:
    store = EpisodeStore(tmp_path / "store")
    session = store.begin(canonical(episodes[0].manifest), robot_id="rbt_1")
    session.write("steps.npz", b"partial")
    EpisodeStore(tmp_path / "store")  # the agent restarted mid-upload
    session.abort()
    assert list((tmp_path / "store" / "incoming").iterdir()) == []
    assert (episodes[0].directory / MANIFEST).exists()  # the robot still has it, to retry
