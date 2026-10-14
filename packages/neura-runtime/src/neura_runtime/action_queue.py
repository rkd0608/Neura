"""The robot's queue of future actions and the chunk merge rules (design §5).

Indexing: within a task epoch every action has a global integer index. ``cursor`` is the index of
the next action to dispatch. Actions below the cursor are immutable history; this class only ever
holds actions at indices ``>= cursor``, so it is structurally impossible to change or re-dispatch
an action that has already been sent to the controller.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

from neura_contracts import ReasonCode

FloatArray = NDArray[np.float64]


class MergeKind(StrEnum):
    REPLACE = "replace"
    APPEND = "append"
    BLEND = "blend"
    RTC = "rtc"


@dataclass(frozen=True, slots=True)
class MergeSettings:
    kind: MergeKind
    actions_per_chunk: int
    min_useful_actions: int
    blend_weights: tuple[float, ...] = ()
    max_conditioned_delay: int = 0


@dataclass(frozen=True, slots=True)
class MergeResult:
    accepted: bool
    reason: ReasonCode | None
    trimmed: int  # actions of the chunk already in the past on arrival
    added: int  # actions of the chunk now queued
    delay: int  # cursor on arrival minus the chunk's start index


class ActionQueue:
    def __init__(self, action_dim: int) -> None:
        self._dim = action_dim
        self._cursor = 0
        self._future: list[FloatArray] = []

    @property
    def cursor(self) -> int:
        return self._cursor

    def __len__(self) -> int:
        return len(self._future)

    @property
    def end_index(self) -> int:
        """Index just after the last queued action."""
        return self._cursor + len(self._future)

    def peek(self, count: int) -> FloatArray:
        rows = self._future[:count]
        if not rows:
            return np.empty((0, self._dim), dtype=np.float64)
        return np.stack(rows)

    def pop(self) -> tuple[int, FloatArray] | None:
        """Take the next action for dispatch. Advances the cursor."""
        if not self._future:
            return None
        index = self._cursor
        action = self._future.pop(0)
        self._cursor += 1
        return index, action

    def clear(self) -> None:
        """Drop all future actions. The cursor is kept: history is never rewritten."""
        self._future.clear()

    def reset_indexing(self) -> None:
        """Start index numbering from zero for a new task epoch."""
        self._future.clear()
        self._cursor = 0

    def merge(self, start: int, actions: FloatArray, settings: MergeSettings) -> MergeResult:
        """Merge a chunk whose first action has index ``start``.

        Only the first ``actions_per_chunk`` actions of a chunk are executable. Indices below the
        cursor are trimmed (never executed late). The merge is atomic: on rejection nothing changes.
        """
        if actions.ndim != 2 or actions.shape[1] != self._dim:
            raise ValueError(f"expected shape (n, {self._dim}), got {actions.shape}")
        delay = self._cursor - start
        executable = actions[: settings.actions_per_chunk]
        trimmed = max(0, delay)
        usable = executable[trimmed:]

        def reject(reason: ReasonCode) -> MergeResult:
            return MergeResult(False, reason, trimmed, 0, delay)

        if settings.kind is MergeKind.RTC and delay > settings.max_conditioned_delay:
            # The model only conditioned on this much delay; its continuation is not valid later.
            return reject(ReasonCode.CHUNK_LATE)
        if len(usable) < settings.min_useful_actions:
            return reject(ReasonCode.CHUNK_LATE)
        first = start + trimmed  # index of usable[0], always >= cursor
        if first > self.end_index:
            return reject(ReasonCode.CHUNK_NOT_CONTIGUOUS)

        keep = first - self._cursor  # queued actions before the chunk that stay untouched
        old_tail = self._future[keep:]
        new_rows = [row.astype(np.float64, copy=True) for row in usable]
        if settings.kind is MergeKind.BLEND:
            for i, weight in enumerate(settings.blend_weights):
                if i >= len(new_rows) or i >= len(old_tail):
                    break
                new_rows[i] = weight * new_rows[i] + (1.0 - weight) * old_tail[i]
        self._future = self._future[:keep] + new_rows
        return MergeResult(True, None, trimmed, len(new_rows), delay)
