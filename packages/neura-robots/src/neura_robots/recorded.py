"""A recorded robot: an episode of a LeRobot dataset (a real arm's joint states and camera frames,
from the Hugging Face Hub or a local copy) played back to the runtime as if a robot were sending
it, at the speed it was recorded.

What it tests is everything but the arm moving in response: the runtime, the transport and the
policy on real observations (real images, real trajectories, real ranges). The commands the
runtime sends change nothing, since the recording goes on as it was recorded, so this is open
loop; ``comparison`` says how far they were from the actions recorded at the same moments.
LeRobot datasets store nominal frame times (frame index / fps), so the playback has no timing
jitter of its own: what jitters is this machine's decoding and the runtime.

Frames are decoded ahead, in order, on their own thread: a frame not decoded by its time is not
served late as if it were current; the newest decoded one keeps its own time and ages, and the
runtime's freshness checks see it. The recording ending is the robot going away: authority is
lost, and the runtime stops. Needs the ``lerobot`` extra (LeRobot reads the dataset).
"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_contracts import FaultResponse, RobotProfile
from neura_robots.so101 import ArmError, Sample
from neura_runtime.interfaces import CommandResult, ImageFrame, SensorSnapshot

LOOKAHEAD_S = 1.0  # how far ahead of playback frames are decoded


@dataclass(slots=True)
class Frame:
    index: int
    state: dict[str, float]
    action: NDArray[np.float64]
    images: dict[str, NDArray[np.uint8]]  # dataset camera key -> H x W x 3, RGB


@dataclass(slots=True)
class Comparison:
    """The commands sent against the actions recorded at the same moments of the episode."""

    commands: int = 0
    abs_error_sum: NDArray[np.float64] = field(default_factory=lambda: np.zeros(0))

    def mean_abs_error(self) -> NDArray[np.float64]:
        return self.abs_error_sum / max(1, self.commands)


class RecordedEpisode:
    """One episode of a LeRobot dataset: its state and action names, fps, task and frames."""

    def __init__(self, dataset: str, episode: int, root: Path | None = None) -> None:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        self.dataset = LeRobotDataset(dataset, root=root, episodes=[episode])
        features = self.dataset.meta.features
        self.fps = float(self.dataset.meta.fps)
        self.length = len(self.dataset)
        self.state_names: list[str] = list(features["observation.state"]["names"])
        self.action_names: list[str] = list(features["action"]["names"])
        self.cameras = {
            key.removeprefix("observation.images."): (int(f["shape"][1]), int(f["shape"][0]))
            for key, f in features.items()
            if key.startswith("observation.images.")
        }  # name -> (width, height)
        self.task = str(self.dataset[0]["task"]) if self.length else ""

    def frame(self, index: int) -> Frame:
        item = self.dataset[index]
        state = np.asarray(item["observation.state"], dtype=np.float64)
        images = {}
        for name in self.cameras:
            chw = np.asarray(item[f"observation.images.{name}"])  # float 0..1, C x H x W
            images[name] = (np.clip(chw, 0, 1) * 255 + 0.5).astype(np.uint8).transpose(1, 2, 0)
        return Frame(
            index,
            dict(zip(self.state_names, (float(v) for v in state), strict=True)),
            np.asarray(item["action"], dtype=np.float64),
            images,
        )


class RecordedArm:
    """``cameras`` maps each camera role of the robot profile to a camera of the dataset."""

    def __init__(
        self, episode: RecordedEpisode, robot: RobotProfile, cameras: Mapping[str, str]
    ) -> None:
        self.episode = episode
        self.cameras = dict(cameras)
        self.profile = robot
        self.counters: Counter[str] = Counter()
        self.telemetry: deque[Sample] = deque(maxlen=100_000)
        self.last_command: NDArray[np.float64] | None = None
        self.comparison = Comparison(abs_error_sum=np.zeros(len(robot.spec.command_interface)))
        self._decoded: deque[Frame] = deque()
        self._ready = threading.Condition()
        self._stop = threading.Event()
        self._decoder: threading.Thread | None = None
        self._start_us: int | None = None
        self._clock: Callable[[], int] = lambda: time.monotonic_ns() // 1000

    # --- lifecycle -----------------------------------------------------------------------------

    def connect(self) -> None:
        problems = self.contract_problems(self.profile)
        if problems:
            raise ArmError(
                "the robot profile does not fit the recording:\n- " + "\n- ".join(problems)
            )
        self._decoder = threading.Thread(target=self._decode, name="recording", daemon=True)
        self._decoder.start()
        with self._ready:  # playback starts with the first frame in hand
            self._ready.wait_for(lambda: bool(self._decoded) or self._stop.is_set(), timeout=60)
        self._start_us = self._clock()

    def contract_problems(self, robot: RobotProfile) -> list[str]:
        spec, episode = robot.spec, self.episode
        problems = []
        for name in [c.name for c in spec.command_interface] + [
            s.name for s in spec.state_features
        ]:
            if name not in episode.state_names:
                problems.append(f"{name}: the recording has no such joint")
        commands = [c.name for c in spec.command_interface]
        if commands != episode.action_names:
            problems.append(f"commands {commands}, the recording's actions {episode.action_names}")
        for camera in spec.cameras:
            source = self.cameras.get(camera.role)
            if source not in episode.cameras:
                problems.append(f"camera {camera.role}: map it to one of {sorted(episode.cameras)}")
            elif episode.cameras[source] != (camera.width, camera.height):
                problems.append(
                    f"camera {camera.role}: {camera.width}x{camera.height}, the recording's "
                    f"{source} is {episode.cameras[source][0]}x{episode.cameras[source][1]}"
                )
        return problems

    def close(self) -> bool:
        self._stop.set()
        with self._ready:
            self._ready.notify_all()
        if self._decoder is not None:
            self._decoder.join(timeout=5)
        return True

    @property
    def ended(self) -> bool:
        return self._index(self._clock()) >= self.episode.length

    # --- playback ------------------------------------------------------------------------------

    def _index(self, now_us: int) -> int:
        if self._start_us is None:
            return 0
        return int((now_us - self._start_us) * self.episode.fps / 1_000_000)

    def _decode(self) -> None:
        for index in range(self.episode.length):
            while not self._stop.is_set():  # no further ahead than the lookahead
                playing = self._index(self._clock())
                if index - playing <= LOOKAHEAD_S * self.episode.fps:
                    break
                time.sleep(0.5 / self.episode.fps)
            if self._stop.is_set():
                return
            frame = self.episode.frame(index)
            with self._ready:
                self._decoded.append(frame)
                self._ready.notify_all()

    def _current(self, now_us: int) -> tuple[Frame, int] | None:
        """The newest decoded frame due by ``now_us``, and the time it was due."""
        due = self._index(now_us)
        with self._ready:
            while len(self._decoded) > 1 and self._decoded[1].index <= due:
                self._decoded.popleft()
            if not self._decoded or self._decoded[0].index > due:
                return None
            frame = self._decoded[0]
        assert self._start_us is not None
        if frame.index < due:
            self.counters["frames_late"] += 1
        return frame, self._start_us + round(frame.index * 1_000_000 / self.episode.fps)

    # --- the runtime's controller and sensors --------------------------------------------------

    def has_authority(self) -> bool:
        return self._start_us is not None and not self.ended

    def command(self, action: NDArray[np.float64]) -> CommandResult:
        if not self.has_authority():
            return CommandResult(False, "the recording has ended")
        now = self._clock()
        current = self._current(now)
        self.counters["commands"] += 1
        self.last_command = np.asarray(action, dtype=np.float64).copy()
        if current is not None:
            self.comparison.commands += 1
            self.comparison.abs_error_sum += np.abs(self.last_command - current[0].action)
            names = [c.name for c in self.profile.spec.command_interface]
            commanded = dict(zip(names, (float(v) for v in action), strict=True))
            self.telemetry.append(Sample(now, current[0].state, commanded))
        return CommandResult(True)

    def apply_response(self, response: FaultResponse) -> bool:
        self.counters[f"response_{response.value}"] += 1
        return response in (FaultResponse.HOLD_POSITION, FaultResponse.CONTROLLED_STOP)

    def capture(self, now_us: int) -> SensorSnapshot | None:
        current = self._current(now_us)
        if current is None:
            return None
        frame, at = current
        state = {s.name: frame.state[s.name] for s in self.profile.spec.state_features}
        images = {}
        for camera in self.profile.spec.cameras:
            image = frame.images[self.cameras[camera.role]]
            data = np.ascontiguousarray(image).tobytes()
            images[camera.role] = ImageFrame(image.shape[1], image.shape[0], data, at)
        return SensorSnapshot(state=state, state_time_us=at, images=images)

    def sample(self, now_us: int | None = None) -> dict[str, float] | None:
        current = self._current(self._clock() if now_us is None else now_us)
        return None if current is None else dict(current[0].state)

    def recover(self) -> None:
        """Nothing to do: the recording goes on as it was recorded."""

    def revalidate(self) -> list[str]:
        problems = self.contract_problems(self.profile)
        if self.ended:
            problems.append("the recording has ended")
        return problems

    def adopt(self, robot: RobotProfile) -> None:
        problems = self.contract_problems(robot)
        if problems:
            raise ArmError(
                "the robot profile does not fit the recording:\n- " + "\n- ".join(problems)
            )
        self.profile = robot


def describe(arm: RecordedArm) -> dict[str, Any]:
    """What a playback did: how far the commands were from the recorded actions, per joint."""
    names = [c.name for c in arm.profile.spec.command_interface]
    error = arm.comparison.mean_abs_error()
    return {
        "dataset": arm.episode.dataset.repo_id,
        "task": arm.episode.task,
        "commands_compared": arm.comparison.commands,
        "mean_abs_error": {n: round(float(e), 3) for n, e in zip(names, error, strict=True)},
        "frames_late": arm.counters["frames_late"],
    }
