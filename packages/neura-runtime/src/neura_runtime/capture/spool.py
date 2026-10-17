"""The robot's episode spool: episodes written to disk by a background thread (design §28.2).

The control loop only hands the writer references (a commanded action, a state vector, a
camera frame's bytes) through a queue bounded in bytes. When the queue is full the item is
dropped and counted, and the episode is marked incomplete: recording never waits, so it can
never delay a command.

Layout under the spool directory, moved between by atomic renames::

    recording/<episode_id>/   being written
    ready/<episode_id>/       closed, waiting for its outcome label and upload
    uploading/<episode_id>/   claimed by the uploader
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import shutil
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_runtime.capture.episode import (
    FORMAT,
    MANIFEST,
    STEPS,
    FramesWriter,
    Steps,
    canonical,
    frames_file,
    sha256_file,
)

log = logging.getLogger("neura.capture")

RECORDING = "recording"
READY = "ready"
UPLOADING = "uploading"


@dataclass(frozen=True, slots=True)
class _Open:
    episode_id: str
    header: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Step:
    episode_id: str
    t_us: int
    index: int
    action: NDArray[np.float64]
    state: NDArray[np.float64]
    frame: int


@dataclass(frozen=True, slots=True)
class _Frames:
    episode_id: str
    images: dict[str, bytes]  # one frame from every recorded camera


@dataclass(frozen=True, slots=True)
class _Close:
    episode_id: str
    trailer: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Label:
    episode_id: str
    outcome: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Discard:
    episode_id: str


@dataclass(frozen=True, slots=True)
class _Flush:
    done: threading.Event
    episode_id: str = ""


_Item = _Open | _Step | _Frames | _Close | _Label | _Discard | _Flush


@dataclass(slots=True)
class _Writing:
    directory: Path
    header: dict[str, Any]
    frames: dict[str, FramesWriter]
    t_us: list[int] = field(default_factory=list)
    index: list[int] = field(default_factory=list)
    action: list[NDArray[np.float64]] = field(default_factory=list)
    state: list[NDArray[np.float64]] = field(default_factory=list)
    frame: list[int] = field(default_factory=list)
    # Bytes added to the spool's total so far (an estimate until the episode closes).
    counted: int = 0


def _replace_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        handle.write(canonical(data))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def directory_bytes(directory: Path) -> int:
    total = 0
    for root, _, files in os.walk(directory):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(root) / name).stat().st_size
    return total


class Spool:
    """Episodes on the robot's disk, and the thread that writes them."""

    def __init__(
        self, directory: Path, *, max_bytes: int, queue_max_bytes: int = 64 * 1024 * 1024
    ) -> None:
        self.directory = directory
        self.max_bytes = max_bytes
        self.queue_max_bytes = queue_max_bytes
        for name in (RECORDING, READY, UPLOADING):
            (directory / name).mkdir(parents=True, exist_ok=True)
        self.counters: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._queued_bytes = 0
        self._queue: queue.SimpleQueue[tuple[_Item | None, int]] = queue.SimpleQueue()  # None: stop
        self._writing: dict[str, _Writing] = {}
        self._recover()
        self._used = directory_bytes(directory)
        self._thread = threading.Thread(target=self._run, name="neura-capture-writer", daemon=True)
        self._thread.start()

    # --- used from the control loop (never blocks) ----------------------------------------------

    def full(self) -> bool:
        with self._lock:
            return self._used >= self.max_bytes

    def open(self, episode_id: str, header: dict[str, Any]) -> None:
        self._put(_Open(episode_id, header), 0, essential=True)

    def step(
        self,
        episode_id: str,
        t_us: int,
        index: int,
        action: NDArray[np.float64],
        state: NDArray[np.float64],
        frame: int,
    ) -> bool:
        size = action.nbytes + state.nbytes + 24
        return self._put(_Step(episode_id, t_us, index, action, state, frame), size)

    def frames(self, episode_id: str, images: dict[str, bytes]) -> bool:
        """One frame from every recorded camera, all or none: the cameras' files stay aligned."""
        return self._put(_Frames(episode_id, images), sum(len(d) for d in images.values()))

    def close(self, episode_id: str, trailer: dict[str, Any]) -> None:
        self._put(_Close(episode_id, trailer), 0, essential=True)

    def label(self, episode_id: str, outcome: dict[str, Any]) -> None:
        self._put(_Label(episode_id, outcome), 0, essential=True)

    def discard(self, episode_id: str) -> None:
        self._put(_Discard(episode_id), 0, essential=True)

    def flush(self, timeout_s: float = 10.0) -> bool:
        """Wait until everything queued so far is written."""
        done = threading.Event()
        self._put(_Flush(done), 0, essential=True)
        return done.wait(timeout_s)

    def _put(self, item: _Item, size: int, *, essential: bool = False) -> bool:
        with self._lock:
            if not essential and self._queued_bytes + size > self.queue_max_bytes:
                self.counters["dropped"] += 1
                return False
            self._queued_bytes += size
        self._queue.put((item, size))
        return True

    # --- used by the uploader --------------------------------------------------------------------

    def ready(self) -> list[Path]:
        """Closed episodes, oldest first."""
        entries = [p for p in (self.directory / READY).iterdir() if (p / MANIFEST).is_file()]
        return sorted(entries, key=lambda p: (p / MANIFEST).stat().st_mtime_ns)

    def claim(self, episode: Path) -> Path | None:
        """Move a ready episode to uploading; None if it moved meanwhile."""
        target = self.directory / UPLOADING / episode.name
        try:
            episode.rename(target)
        except OSError:
            return None
        return target

    def release(self, episode: Path) -> None:
        """An upload that failed: back to ready, to try again."""
        with contextlib.suppress(OSError):
            episode.rename(self.directory / READY / episode.name)

    def remove(self, episode: Path) -> None:
        size = directory_bytes(episode)
        shutil.rmtree(episode, ignore_errors=True)
        with self._lock:
            self._used = max(0, self._used - size)

    def stop(self, timeout_s: float = 10.0) -> None:
        self._queue.put((None, 0))
        self._thread.join(timeout_s)

    # --- the writer thread ------------------------------------------------------------------------

    def _recover(self) -> None:
        """At start: an episode left recording by a crash cannot be completed; an upload cut
        short goes back to ready."""
        for partial in (self.directory / RECORDING).iterdir():
            shutil.rmtree(partial, ignore_errors=True)
            self.counters["abandoned"] += 1
        for claimed in (self.directory / UPLOADING).iterdir():
            self.release(claimed)

    def _run(self) -> None:
        while True:
            item, size = self._queue.get()
            with self._lock:
                self._queued_bytes -= size
            if item is None:
                for episode_id in list(self._writing):
                    self._discard(episode_id)
                return
            try:
                self._handle(item)
            except Exception:
                log.exception("capture writer failed on %s", type(item).__name__)
                self.counters["writer_failures"] += 1
                if item.episode_id in self._writing:
                    self._discard(item.episode_id)

    def _handle(self, item: _Item) -> None:
        if isinstance(item, _Open):
            directory = self.directory / RECORDING / item.episode_id
            directory.mkdir()
            writers = {
                camera["role"]: FramesWriter(directory / frames_file(camera["role"]))
                for camera in item.header["cameras"]
            }
            self._writing[item.episode_id] = _Writing(directory, item.header, writers)
        elif isinstance(item, _Step):
            writing = self._writing.get(item.episode_id)
            if writing is not None:
                writing.t_us.append(item.t_us)
                writing.index.append(item.index)
                writing.action.append(item.action)
                writing.state.append(item.state)
                writing.frame.append(item.frame)
        elif isinstance(item, _Frames):
            writing = self._writing.get(item.episode_id)
            if writing is not None:
                for role, frames in writing.frames.items():
                    frames.append(item.images[role])
                estimate = sum(len(d) for d in item.images.values()) // 4  # about what zlib leaves
                writing.counted += estimate
                self._grow(estimate)
        elif isinstance(item, _Close):
            self._finish(item.episode_id, item.trailer)
        elif isinstance(item, _Label):
            self._relabel(item.episode_id, item.outcome)
        elif isinstance(item, _Discard):
            self._discard(item.episode_id)
        elif isinstance(item, _Flush):
            item.done.set()

    def _grow(self, size: int) -> None:
        with self._lock:
            self._used = max(0, self._used + size)

    def _discard(self, episode_id: str) -> None:
        writing = self._writing.pop(episode_id, None)
        if writing is None:
            return
        for frames in writing.frames.values():
            with contextlib.suppress(OSError):
                frames.close()
        shutil.rmtree(writing.directory, ignore_errors=True)
        self._grow(-writing.counted)

    def _finish(self, episode_id: str, trailer: dict[str, Any]) -> None:
        writing = self._writing.pop(episode_id, None)
        if writing is None:
            return
        header = writing.header
        n_state = len(header["state_names"])
        n_action = len(header["command_names"])
        steps = Steps(
            t_us=np.array(writing.t_us, dtype=np.int64),
            action_index=np.array(writing.index, dtype=np.int64),
            action=np.array(writing.action, dtype=np.float64).reshape(-1, n_action),
            state=np.array(writing.state, dtype=np.float64).reshape(-1, n_state),
            frame=np.array(writing.frame, dtype=np.int64),
            frame_offsets={
                role: np.array(frames.offsets, dtype=np.int64)
                for role, frames in writing.frames.items()
            },
        )
        for frames in writing.frames.values():
            frames.close()
        steps.save(writing.directory / STEPS)
        files = {
            name: {"sha256": sha256_file(writing.directory / name), "bytes": size}
            for name in [STEPS, *(frames_file(role) for role in writing.frames)]
            for size in [(writing.directory / name).stat().st_size]
        }
        manifest = {
            **header,
            **trailer,
            "format": FORMAT,
            "steps": len(steps.t_us),
            "frames": int(max((len(f.offsets) for f in writing.frames.values()), default=0)),
            "files": files,
        }
        _replace_json(writing.directory / MANIFEST, manifest)
        target = self.directory / READY / episode_id
        writing.directory.rename(target)
        self._grow(directory_bytes(target) - writing.counted)
        self.counters["episodes"] += 1

    def _relabel(self, episode_id: str, outcome: dict[str, Any]) -> None:
        """Label a closed episode (the recorder labels an open one itself)."""
        path = self.directory / READY / episode_id / MANIFEST
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            # Uploaded (or uploading) already: the label belongs at the site now.
            self.counters["late_labels"] += 1
            log.warning("episode %s was already uploaded; label it at the site", episode_id)
            return
        manifest["outcome"] = outcome
        _replace_json(path, manifest)
