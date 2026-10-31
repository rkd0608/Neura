"""The episode format a robot records and the site stores (design §28.2): ``neura-episode/1``.

An episode is one directory::

    episode.json          the manifest, written last: who, what ran, timing, cameras, outcome,
                          events, and every other file's SHA-256 and size
    steps.npz             per control period: time, action index, commanded action (robot order
                          and units), robot state, and the camera frame recorded then (or -1)
    camera-<role>.frames  that camera's frames, each zlib-compressed raw HxWx3 in the camera's
                          declared color order, behind a 4-byte little-endian length

Robot units and order throughout: the site maps them to a model's units when it exports a
dataset, with the execution profile the episode ran under. Nothing here needs a model library,
so the format is read and written on robot computers as well as the site.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_contracts.compat import StrEnum

FORMAT = "neura-episode/1"
MANIFEST = "episode.json"
STEPS = "steps.npz"
FRAMES_MAGIC = b"NEFRAMES1\n"
_LENGTH = struct.Struct("<I")
# Raw frames compress quickly at level 1; the robot computer's CPU belongs to the control loop.
ZLIB_LEVEL = 1


class EndReason(StrEnum):
    """Why an episode ended."""

    STOPPED = "stopped"  # the task ended at a boundary (operator stop or the integration)
    TAKEOVER = "takeover"  # the operator took authority from the policy
    FAULT = "fault"  # a fault latched
    NEW_EPOCH = "new_epoch"  # the task epoch advanced while running (instruction, starvation)
    MAX_DURATION = "max_duration"  # split: the task continues in the next episode
    DISCONNECTED = "disconnected"  # the robot process ended mid-task


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class OutcomeSource(StrEnum):
    ROBOT = "robot"  # the integration reported it (a task sensor, a simulator's reward)
    OPERATOR = "operator"  # a person labeled it, at the robot or at the site
    RULE = "rule"  # inferred: a fault or a takeover is a failure


# Episodes worth learning from when capture records failures only.
FAILURE_ENDS = frozenset({EndReason.FAULT, EndReason.TAKEOVER})


def is_failure(manifest: dict[str, Any]) -> bool:
    outcome = manifest.get("outcome", {}).get("label")
    return outcome == Outcome.FAILURE or manifest.get("end", {}).get("reason") in FAILURE_ENDS


def canonical(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def frames_file(role: str) -> str:
    return f"camera-{role}.frames"


class FramesWriter:
    """Appends compressed frames to a camera's file; returns each frame's offset."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = path.open("wb")
        self._handle.write(FRAMES_MAGIC)
        self.offsets: list[int] = []

    def append(self, raw: bytes) -> int:
        offset = self._handle.tell()
        data = zlib.compress(raw, ZLIB_LEVEL)
        self._handle.write(_LENGTH.pack(len(data)))
        self._handle.write(data)
        self.offsets.append(offset)
        return len(self.offsets) - 1

    def close(self) -> None:
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()


def read_frame(path: Path, offset: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        (length,) = _LENGTH.unpack(handle.read(_LENGTH.size))
        return zlib.decompress(handle.read(length))


def iter_frames(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        if handle.read(len(FRAMES_MAGIC)) != FRAMES_MAGIC:
            raise EpisodeError(f"{path.name} is not a frames file")
        while header := handle.read(_LENGTH.size):
            (length,) = _LENGTH.unpack(header)
            yield zlib.decompress(handle.read(length))


class EpisodeError(ValueError):
    """An episode directory that is not a complete, intact ``neura-episode/1`` episode."""


@dataclass(slots=True)
class Steps:
    t_us: NDArray[np.int64]  # since the episode started, robot clock
    action_index: NDArray[np.int64]
    action: NDArray[np.float64]  # [steps, commands], robot order and units
    state: NDArray[np.float64]  # [steps, state features], NaN where a reading was missing
    frame: NDArray[np.int64]  # [steps], index into each camera's frames, -1 for none
    frame_offsets: dict[str, NDArray[np.int64]] = field(default_factory=dict)

    def save(self, path: Path) -> None:
        buffer = io.BytesIO()
        offsets = {f"offsets_{role}": values for role, values in self.frame_offsets.items()}
        np.savez(
            buffer,
            t_us=self.t_us,
            action_index=self.action_index,
            action=self.action,
            state=self.state,
            frame=self.frame,
            **offsets,  # type: ignore[arg-type]
        )
        with path.open("wb") as handle:
            handle.write(buffer.getvalue())
            handle.flush()
            os.fsync(handle.fileno())

    @classmethod
    def load(cls, path: Path) -> Steps:
        with np.load(path, allow_pickle=False) as data:
            return cls(
                t_us=data["t_us"],
                action_index=data["action_index"],
                action=data["action"],
                state=data["state"],
                frame=data["frame"],
                frame_offsets={
                    name.removeprefix("offsets_"): data[name]
                    for name in data.files
                    if name.startswith("offsets_")
                },
            )


@dataclass(frozen=True, slots=True)
class Episode:
    """A complete episode on disk, verified."""

    directory: Path
    manifest: dict[str, Any]

    @property
    def episode_id(self) -> str:
        return str(self.manifest["episode_id"])

    def steps(self) -> Steps:
        return Steps.load(self.directory / STEPS)

    def frame(self, role: str, index: int) -> NDArray[np.uint8]:
        """One recorded frame, HxWx3 in the camera's color order."""
        camera = next(c for c in self.manifest["cameras"] if c["role"] == role)
        offsets = self.steps().frame_offsets[role]
        raw = read_frame(self.directory / frames_file(role), int(offsets[index]))
        return np.frombuffer(raw, np.uint8).reshape(camera["height"], camera["width"], 3)


def open_episode(directory: Path, *, verify: bool = True) -> Episode:
    """Read and check an episode: the manifest, its format, and (by default) every file's
    digest and size."""
    path = directory / MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EpisodeError(f"{directory.name}: no readable {MANIFEST}: {exc}") from exc
    check_manifest(manifest)
    if verify:
        for name, entry in manifest["files"].items():
            file = directory / name
            if not file.is_file():
                raise EpisodeError(f"{directory.name}: {name} is missing")
            if file.stat().st_size != entry["bytes"] or sha256_file(file) != entry["sha256"]:
                raise EpisodeError(f"{directory.name}: {name} does not match its digest")
    return Episode(directory, manifest)


REQUIRED = (
    "format",
    "episode_id",
    "robot_id",
    "revision_id",
    "artifact_digest",
    "execution_profile_digest",
    "robot_profile_digest",
    "control_period_us",
    "steps",
    "state_names",
    "command_names",
    "cameras",
    "end",
    "outcome",
    "files",
)


def check_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise EpisodeError("the manifest is not an object")
    missing = [key for key in REQUIRED if key not in manifest]
    if missing:
        raise EpisodeError(f"the manifest lacks {', '.join(missing)}")
    if manifest["format"] != FORMAT:
        raise EpisodeError(f"format {manifest['format']!r} is not {FORMAT}")
    episode_id = manifest["episode_id"]
    if not isinstance(episode_id, str) or not episode_id.startswith("ep_") or len(episode_id) > 64:
        raise EpisodeError("episode_id must be ep_ followed by an identifier")
    if not all(c.isalnum() or c == "_" for c in episode_id):
        raise EpisodeError("episode_id has characters other than letters, digits and _")
    files = manifest["files"]
    if not isinstance(files, dict) or STEPS not in files:
        raise EpisodeError(f"the manifest's files must include {STEPS}")
    allowed = {STEPS} | {frames_file(c["role"]) for c in manifest["cameras"]}
    for name, entry in files.items():
        if name not in allowed:
            raise EpisodeError(f"the manifest names an unexpected file {name!r}")
        if not isinstance(entry, dict) or not isinstance(entry.get("bytes"), int):
            raise EpisodeError(f"{name}: the manifest needs its size")
        if not isinstance(entry.get("sha256"), str) or len(entry["sha256"]) != 64:
            raise EpisodeError(f"{name}: the manifest needs its SHA-256")
    if manifest["outcome"].get("label") not in {o.value for o in Outcome}:
        raise EpisodeError("the outcome label is not success, failure or unknown")
    if manifest["end"].get("reason") not in {r.value for r in EndReason}:
        raise EpisodeError("the end reason is not one this format defines")
