"""Episode recording on the robot (design §28.2).

``EpisodeRecorder`` watches the runtime (``RuntimeObserver``) and is ticked by the integration
after each control period. An episode is one task while the runtime is active: it opens when
the runtime arms, and closes when the task stops, the operator takes over, a fault latches, the
epoch advances, or it reaches its maximum length (the task then continues in a new episode).

Every period with a command records the commanded action and the robot's state; camera frames
are recorded every ``image_stride`` periods. All of it is handed to the spool's writer thread
without waiting: what does not fit in the writer's queue is dropped and counted, and the
episode is marked incomplete.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura.v1 import capture_pb2
from neura_contracts import ReasonCode
from neura_runtime.capture.episode import EndReason, Outcome, OutcomeSource
from neura_runtime.capture.spool import Spool
from neura_runtime.interfaces import SensorSnapshot, SensorSource
from neura_runtime.runtime import ACTIVE_STATES, FaultRecord, RobotRuntime, RuntimeState

MAX_EVENTS = 20_000
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_episode_id() -> str:
    """``ep_`` and a ULID: time-ordered, so episodes sort by when they started."""
    value = (time.time_ns() // 1_000_000) << 80 | int.from_bytes(os.urandom(10), "big")
    text = "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))
    return f"ep_{text}"


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    """What to record, from the site's ``CaptureSettings`` (``Assignment.capture``)."""

    mode: str  # "all" or "failures"
    endpoint: str
    image_stride: int = 1
    cameras: tuple[str, ...] = ()
    max_episode_seconds: int = 600
    spool_max_bytes: int = 2 * 1024**3
    upload_bytes_per_second: int = 0

    @classmethod
    def from_proto(cls, settings: capture_pb2.CaptureSettings) -> CaptureConfig | None:
        """None when the site records nothing for this revision."""
        modes = {capture_pb2.CAPTURE_MODE_ALL: "all", capture_pb2.CAPTURE_MODE_FAILURES: "failures"}
        mode = modes.get(settings.mode)
        if mode is None:
            return None
        return cls(
            mode=mode,
            endpoint=settings.endpoint,
            image_stride=max(1, settings.image_stride or 1),
            cameras=tuple(settings.cameras),
            max_episode_seconds=settings.max_episode_seconds or 600,
            spool_max_bytes=settings.spool_max_bytes or 2 * 1024**3,
            upload_bytes_per_second=settings.upload_bytes_per_second,
        )


@dataclass(slots=True)
class _Episode:
    episode_id: str
    task_epoch: int
    started_us: int
    part: int
    steps: int = 0
    frames: int = 0
    incomplete: bool = False
    events: list[dict[str, Any]] | None = None
    outcome: dict[str, Any] | None = None
    counters: Counter[str] | None = None


StateSource = Callable[[int], dict[str, float] | None]
FrameSource = Callable[[int], SensorSnapshot | None]


class EpisodeRecorder:
    def __init__(
        self,
        runtime: RobotRuntime,
        config: CaptureConfig,
        spool: Spool,
        *,
        robot_id: str,
        revision_id: str,
        sensors: SensorSource,
        state_source: StateSource | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        binding = runtime.binding
        self.runtime = runtime
        self.config = config
        self.spool = spool
        self.robot_id = robot_id
        self.revision_id = revision_id
        self._frames: FrameSource = sensors.capture
        self._state: StateSource = state_source or self._state_from_sensors
        self._wall = wall_clock
        spec = binding.robot.spec
        self.state_names = [f.name for f in spec.state_features]
        self.command_names = [c.name for c in spec.command_interface]
        recorded = set(config.cameras) if config.cameras else None
        self.cameras: list[dict[str, Any]] = [
            {
                "role": camera.role,
                "width": camera.width,
                "height": camera.height,
                "color_order": camera.color_order.value,
            }
            for camera in spec.cameras
            if (recorded is None and camera.role in {i.camera_role for i in binding.image_layouts})
            or (recorded is not None and camera.role in recorded)
        ]
        self.counters: Counter[str] = Counter()
        self._open: _Episode | None = None
        self._last: str | None = None  # the most recently closed episode, for late labels
        self._cause: ReasonCode | None = None
        self._fault: FaultRecord | None = None
        self._skipped: int | None = None  # a task epoch left unrecorded
        self._attached = False

    # --- lifecycle --------------------------------------------------------------------------------

    def attach(self) -> None:
        if not self._attached:
            self.runtime.observers.append(self)
            self._attached = True

    def detach(self, now_us: int) -> None:
        """Stop recording: an episode still open ends as disconnected."""
        if self._open is not None:
            self._close(now_us, EndReason.DISCONNECTED)
        if self._attached:
            if self in self.runtime.observers:
                self.runtime.observers.remove(self)
            self._attached = False

    @property
    def episode_id(self) -> str | None:
        return self._open.episode_id if self._open else None

    def tick(self, now_us: int) -> None:
        """After each control period: open, close or split the episode."""
        runtime = self.runtime
        episode = self._open
        if episode is not None:
            if runtime.state is RuntimeState.FAULTED:
                self._close(now_us, EndReason.FAULT)
            elif runtime.task_epoch != episode.task_epoch:
                if self._cause is ReasonCode.OPERATOR_TAKEOVER:
                    reason = EndReason.TAKEOVER
                elif runtime.state in ACTIVE_STATES:
                    reason = EndReason.NEW_EPOCH
                else:
                    reason = EndReason.STOPPED
                self._close(now_us, reason)
            elif runtime.state not in ACTIVE_STATES:
                self._close(now_us, EndReason.STOPPED)
            elif now_us - episode.started_us >= self.config.max_episode_seconds * 1_000_000:
                self._close(now_us, EndReason.MAX_DURATION)
                self._begin(now_us, part=episode.part + 1)
                return
        if self._open is None and runtime.state in ACTIVE_STATES:
            self._begin(now_us, part=0)

    def label(
        self, outcome: Outcome, source: OutcomeSource = OutcomeSource.ROBOT, note: str = ""
    ) -> str | None:
        """Label the open episode, or else the one that just closed. Returns its id."""
        record = {
            "label": outcome.value,
            "source": source.value,
            "at": _iso(self._wall()),
            **({"note": note[:500]} if note else {}),
        }
        if self._open is not None:
            self._open.outcome = record
            return self._open.episode_id
        if self._last is not None:
            self.spool.label(self._last, record)
            return self._last
        return None

    # --- RuntimeObserver ---------------------------------------------------------------------

    def on_command(
        self, now_us: int, task_epoch: int, index: int, action: NDArray[np.float64]
    ) -> None:
        episode = self._open
        if episode is None or task_epoch != episode.task_epoch:
            return
        state = self._state(now_us)
        values = np.full(len(self.state_names), np.nan)
        if state is not None:
            for i, name in enumerate(self.state_names):
                value = state.get(name)
                if value is not None:
                    values[i] = value
        frame = -1
        if self.cameras and episode.steps % self.config.image_stride == 0:
            frame = self._record_frames(episode, now_us)
        if not self.spool.step(
            episode.episode_id,
            now_us - episode.started_us,
            index,
            np.array(action, dtype=np.float64),
            values,
            frame,
        ):
            episode.incomplete = True
            self.counters["steps_dropped"] += 1
        episode.steps += 1

    def on_chunk(
        self,
        now_us: int,
        task_epoch: int,
        sequence: int,
        start_index: int,
        accepted: bool,
        reason: ReasonCode | None,
        trimmed: int,
    ) -> None:
        self._event(
            now_us,
            {
                "kind": "chunk",
                "sequence": sequence,
                "start_index": start_index,
                "accepted": accepted,
                **({"reason": reason.value} if reason is not None else {}),
                **({"trimmed": trimmed} if trimmed else {}),
            },
        )

    def on_epoch(self, previous: int, current: int, cause: ReasonCode | None) -> None:
        self._cause = cause

    def on_fault(self, fault: FaultRecord) -> None:
        self._fault = fault
        self._event(
            fault.at_us,
            {
                "kind": "fault",
                "reason": fault.reason.value,
                **({"detail": fault.detail[:200]} if fault.detail else {}),
            },
        )

    # --- internals ---------------------------------------------------------------------------

    def _state_from_sensors(self, now_us: int) -> dict[str, float] | None:
        snapshot = self._frames(now_us)
        return snapshot.state if snapshot is not None else None

    def _record_frames(self, episode: _Episode, now_us: int) -> int:
        snapshot = self._frames(now_us)
        if snapshot is None or any(c["role"] not in snapshot.images for c in self.cameras):
            self.counters["frames_missing"] += 1
            return -1
        images: dict[str, bytes] = {}
        for camera in self.cameras:
            image = snapshot.images[camera["role"]]
            expected = camera["width"] * camera["height"] * 3
            if (image.width, image.height) != (camera["width"], camera["height"]) or len(
                image.data
            ) != expected:
                self.counters["frames_missing"] += 1
                return -1
            images[camera["role"]] = image.data
        if not self.spool.frames(episode.episode_id, images):
            episode.incomplete = True
            self.counters["frames_dropped"] += 1
            return -1
        episode.frames += 1
        return episode.frames - 1

    def _event(self, now_us: int, event: dict[str, Any]) -> None:
        episode = self._open
        if episode is None or episode.events is None:
            return
        if len(episode.events) >= MAX_EVENTS:
            episode.counters["events_dropped"] += 1  # type: ignore[index]
            return
        episode.events.append({"t_us": max(0, now_us - episode.started_us), **event})

    def _begin(self, now_us: int, *, part: int) -> None:
        epoch = self.runtime.task_epoch
        if self._skipped == epoch:
            return
        if self.spool.full():
            self._skipped = epoch  # this task goes unrecorded; the next one tries again
            self.counters["episodes_skipped_spool_full"] += 1
            return
        binding = self.runtime.binding
        episode = _Episode(
            new_episode_id(),
            self.runtime.task_epoch,
            now_us,
            part,
            events=[],
            counters=Counter(),
        )
        self._cause = None
        self._fault = None
        self.spool.open(
            episode.episode_id,
            {
                "episode_id": episode.episode_id,
                "robot_id": self.robot_id,
                "revision_id": self.revision_id,
                "artifact_digest": binding.artifact_digest,
                "execution_profile_digest": binding.execution_profile_digest,
                "robot_profile_digest": binding.robot_profile_digest,
                "session_id": self.runtime.session_id or "",
                "task_epoch": episode.task_epoch,
                "part": part,
                "capture_mode": self.config.mode,
                "started_at": _iso(self._wall()),
                "control_period_us": binding.control_period_us,
                "image_stride": self.config.image_stride,
                "instruction": self.runtime.instruction,
                "state_names": self.state_names,
                "command_names": self.command_names,
                "cameras": self.cameras,
            },
        )
        self._open = episode
        self.counters["episodes"] += 1

    def _close(self, now_us: int, reason: EndReason) -> None:
        episode = self._open
        if episode is None:
            return
        self._open = None
        end: dict[str, Any] = {"reason": reason.value}
        if reason is EndReason.FAULT and self._fault is not None:
            end["fault_reason"] = self._fault.reason.value
        outcome = episode.outcome
        if outcome is None:
            failed = reason in (EndReason.FAULT, EndReason.TAKEOVER)
            outcome = {
                "label": (Outcome.FAILURE if failed else Outcome.UNKNOWN).value,
                "source": (OutcomeSource.RULE if failed else OutcomeSource.ROBOT).value,
                "at": _iso(self._wall()),
            }
        self.spool.close(
            episode.episode_id,
            {
                "ended_at": _iso(self._wall()),
                "duration_s": round((now_us - episode.started_us) / 1e6, 3),
                "end": end,
                "outcome": outcome,
                "complete": not episode.incomplete,
                "events": episode.events or [],
                "counters": dict(episode.counters or {}),
            },
        )
        self._last = episode.episode_id
