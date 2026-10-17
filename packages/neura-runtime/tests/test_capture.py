"""Episode capture on the robot: recording never blocks the control loop, episodes follow the
task, and finished episodes upload to the site exactly once."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from neura.v1 import capture_pb2
from neura_contracts import ReasonCode
from neura_runtime.capture import (
    CaptureConfig,
    EndReason,
    EpisodeRecorder,
    Outcome,
    OutcomeSource,
    Spool,
    Uploader,
    open_episode,
)
from neura_runtime.capture.episode import MANIFEST, iter_frames
from neura_runtime.capture.uploader import capture_target
from neura_runtime.runtime import RobotRuntime, RuntimeState, SendObservation
from neura_runtime.testing import PERIOD
from neura_runtime.testing import RuntimeHarness as Harness

CONFIG = CaptureConfig(mode="all", endpoint=":50052", image_stride=2)


class Recording:
    def __init__(self, harness: Harness, spool: Spool, config: CaptureConfig = CONFIG) -> None:
        self.harness = harness
        self.spool = spool
        self.recorder = EpisodeRecorder(
            harness.runtime,
            config,
            spool,
            robot_id="rbt_1",
            revision_id="rev_1",
            sensors=harness.sensors,
        )
        self.recorder.attach()

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.harness.tick()
            self.recorder.tick(self.harness.now)

    def run_task(self, ticks: int) -> None:
        """Arm, answer the first request with a chunk, and run."""
        observation = self.harness.start()
        self.recorder.tick(self.harness.now)
        assert self.harness.runtime.on_chunk(self.harness.chunk_for(observation), self.harness.now)
        self.tick(ticks)

    def serve(self, ticks: int) -> None:
        """Arm and run, answering every request at once."""
        harness = self.harness
        self.run_task(0)
        for _ in range(ticks):
            harness.now += PERIOD
            harness.runtime.tick(harness.now)
            for item in harness.runtime.outbox:
                if isinstance(item, SendObservation):
                    harness.runtime.on_chunk(harness.chunk_for(item.message), harness.now)
            harness.runtime.outbox.clear()
            self.recorder.tick(harness.now)

    def episodes(self) -> list[Any]:
        assert self.spool.flush()
        return [open_episode(p) for p in self.spool.ready()]


@pytest.fixture
def harness(make_binding: Any) -> Harness:
    return Harness(make_binding())


@pytest.fixture
def spool(tmp_path: Path) -> Iterator[Spool]:
    spool = Spool(tmp_path / "spool", max_bytes=1 << 30)
    yield spool
    spool.stop()


def test_an_episode_is_one_task_with_every_command_and_strided_frames(
    harness: Harness, spool: Spool
) -> None:
    recording = Recording(harness, spool)
    recording.run_task(10)
    harness.runtime.complete_task(harness.now)
    recording.tick()
    (episode,) = recording.episodes()
    manifest = episode.manifest
    assert manifest["end"] == {"reason": EndReason.STOPPED}
    assert manifest["outcome"]["label"] == Outcome.UNKNOWN
    assert manifest["complete"] is True
    assert manifest["artifact_digest"] == harness.binding.artifact_digest
    assert [c["role"] for c in manifest["cameras"]] == ["front", "wrist"]
    steps = episode.steps()
    commands = harness.controller.commands
    assert manifest["steps"] == len(commands) == 10
    np.testing.assert_array_equal(steps.action, np.array(commands))
    assert np.all(steps.state == 1.0)  # what the sensors read
    assert list(steps.frame) == [0, -1, 1, -1, 2, -1, 3, -1, 4, -1]  # every second period
    assert manifest["frames"] == 5
    assert episode.frame("wrist", 4).shape == (480, 640, 3)
    assert len(list(iter_frames(episode.directory / "camera-front.frames"))) == 5
    chunk_events = [e for e in manifest["events"] if e["kind"] == "chunk"]
    assert chunk_events
    assert chunk_events[0]["accepted"] is True


def test_takeover_and_faults_end_episodes_as_failures(harness: Harness, spool: Spool) -> None:
    recording = Recording(harness, spool)
    recording.run_task(3)
    harness.runtime.operator_takeover(harness.now)
    recording.tick()
    harness.runtime.arm(harness.now)
    observation = harness.tick_for_observation()
    recording.recorder.tick(harness.now)
    assert harness.runtime.on_chunk(harness.chunk_for(observation), harness.now)
    harness.controller.fail = True  # the controller refuses the next command: a fault
    recording.tick(2)
    assert harness.runtime.state is RuntimeState.FAULTED
    first, second = recording.episodes()
    assert first.manifest["end"]["reason"] == EndReason.TAKEOVER
    assert first.manifest["outcome"] == {
        **first.manifest["outcome"],
        "label": Outcome.FAILURE,
        "source": OutcomeSource.RULE,
    }
    assert second.manifest["end"] == {
        "reason": EndReason.FAULT,
        "fault_reason": ReasonCode.CONTROLLER_COMMAND_FAILED,
    }


def test_labels_reach_the_open_episode_or_the_one_just_closed(
    harness: Harness, spool: Spool
) -> None:
    recording = Recording(harness, spool)
    recording.run_task(2)
    recording.recorder.label(Outcome.SUCCESS)
    harness.runtime.complete_task(harness.now)
    recording.tick()
    recording.tick()
    (episode,) = recording.episodes()
    assert episode.manifest["outcome"]["label"] == Outcome.SUCCESS
    # After the episode closed: the spool relabels it before upload.
    recording.recorder.label(Outcome.FAILURE, OutcomeSource.OPERATOR, note="dropped the cube")
    (episode,) = recording.episodes()
    assert episode.manifest["outcome"]["label"] == Outcome.FAILURE
    assert episode.manifest["outcome"]["note"] == "dropped the cube"


def test_long_tasks_split_and_continue(harness: Harness, spool: Spool) -> None:
    config = CaptureConfig(mode="all", endpoint=":1", max_episode_seconds=1)
    recording = Recording(harness, spool, config)
    recording.serve(int(2.5e6 / PERIOD))
    harness.runtime.complete_task(harness.now)
    recording.tick()
    episodes = recording.episodes()
    assert [e.manifest["end"]["reason"] for e in episodes] == [
        EndReason.MAX_DURATION,
        EndReason.MAX_DURATION,
        EndReason.STOPPED,
    ]
    assert [e.manifest["part"] for e in episodes] == [0, 1, 2]
    assert len({e.manifest["task_epoch"] for e in episodes}) == 1
    assert sum(e.manifest["steps"] for e in episodes) == len(harness.controller.commands)


def test_a_full_writer_queue_drops_frames_never_commands(harness: Harness, tmp_path: Path) -> None:
    spool = Spool(tmp_path / "spool", max_bytes=1 << 30, queue_max_bytes=1)
    try:
        recording = Recording(harness, spool, CaptureConfig(mode="all", endpoint=":1"))
        recording.run_task(6)
        assert len(harness.controller.commands) == 6  # the robot ran every period regardless
        harness.runtime.complete_task(harness.now)
        recording.tick()
        (episode,) = recording.episodes()
        assert episode.manifest["complete"] is False
        assert recording.recorder.counters["frames_dropped"] > 0
    finally:
        spool.stop()


def test_a_full_spool_records_nothing_new(harness: Harness, tmp_path: Path) -> None:
    spool = Spool(tmp_path / "spool", max_bytes=0)
    try:
        recording = Recording(harness, spool)
        recording.run_task(3)
        assert recording.recorder.episode_id is None
        assert recording.recorder.counters["episodes_skipped_spool_full"] == 1
        assert recording.episodes() == []
    finally:
        spool.stop()


def test_a_failing_observer_is_detached_and_the_robot_carries_on(harness: Harness) -> None:
    class Broken:
        def on_command(self, *args: object) -> None:
            raise RuntimeError("boom")

    runtime: RobotRuntime = harness.runtime
    runtime.observers.append(Broken())  # type: ignore[arg-type]
    observation = harness.start()
    assert runtime.on_chunk(harness.chunk_for(observation), harness.now)
    harness.tick(3)
    assert len(harness.controller.commands) == 3
    assert runtime.observers == []
    assert runtime.counters["observer_failures"] == 1


def test_capture_settings_from_the_assignment() -> None:
    assert CaptureConfig.from_proto(capture_pb2.CaptureSettings()) is None
    off = capture_pb2.CaptureSettings(mode=capture_pb2.CAPTURE_MODE_OFF)
    assert CaptureConfig.from_proto(off) is None
    config = CaptureConfig.from_proto(
        capture_pb2.CaptureSettings(
            mode=capture_pb2.CAPTURE_MODE_FAILURES, endpoint=":50052", cameras=["front"]
        )
    )
    assert config == CaptureConfig(mode="failures", endpoint=":50052", cameras=("front",))
    assert capture_target(":50052", "site.local:50051") == "site.local:50052"
    assert capture_target("10.0.0.2:7000", "site.local:50051") == "10.0.0.2:7000"


class FakeStub:
    def __init__(self) -> None:
        self.received: dict[str, dict[str, bytes]] = {}
        self.fail = False

    def UploadEpisode(
        self, parts: Any, timeout: float, metadata: Any
    ) -> capture_pb2.EpisodeReceipt:
        files: dict[str, bytes] = {}
        episode_id = ""
        for part in parts:
            if part.HasField("header"):
                episode_id = part.header.episode_id
                files[MANIFEST] = part.header.manifest
            else:
                files[part.data.file] = files.get(part.data.file, b"") + part.data.data
        if self.fail:
            import grpc

            class Unavailable(grpc.RpcError):
                def code(self) -> grpc.StatusCode:
                    return grpc.StatusCode.UNAVAILABLE

            raise Unavailable()
        stored = episode_id not in self.received
        self.received[episode_id] = files
        return capture_pb2.EpisodeReceipt(episode_id=episode_id, stored=stored)


def test_uploads_send_each_episode_once_and_keep_it_until_acknowledged(
    harness: Harness, spool: Spool
) -> None:
    import grpc

    recording = Recording(harness, spool)
    recording.run_task(4)
    harness.runtime.complete_task(harness.now)
    recording.tick()
    (episode,) = recording.episodes()
    stub = FakeStub()
    uploader = Uploader(spool, "unused", lambda target: None, label_grace_s=0)  # type: ignore[arg-type,return-value]
    stub.fail = True
    with pytest.raises(grpc.RpcError):
        uploader.drain(stub)
    assert [p.name for p in spool.ready()] == [episode.episode_id]  # kept for the retry
    stub.fail = False
    assert uploader.drain(stub)
    assert spool.ready() == []
    files = stub.received[episode.episode_id]
    assert json.loads(files[MANIFEST])["episode_id"] == episode.episode_id
    assert set(files) == {MANIFEST, "steps.npz", "camera-front.frames", "camera-wrist.frames"}


def test_failures_mode_uploads_only_failures(harness: Harness, spool: Spool) -> None:
    config = CaptureConfig(mode="failures", endpoint=":1")
    recording = Recording(harness, spool, config)
    recording.run_task(2)
    harness.runtime.complete_task(harness.now)  # a plain stop: not a failure
    recording.tick()
    harness.runtime.arm(harness.now)
    recording.tick()
    harness.runtime.operator_takeover(harness.now)  # a takeover: a failure
    recording.tick()
    assert len(recording.episodes()) == 2
    stub = FakeStub()
    uploader = Uploader(spool, "unused", lambda target: None, label_grace_s=0)  # type: ignore[arg-type,return-value]
    uploader.drain(stub)
    (uploaded,) = stub.received.values()
    assert json.loads(uploaded[MANIFEST])["end"]["reason"] == EndReason.TAKEOVER
    assert uploader.counters["not_failures"] == 1
    assert spool.ready() == []
