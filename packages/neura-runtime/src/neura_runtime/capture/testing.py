"""Synthetic episodes for tests of anything that reads them (site store, export, training,
replay evaluation), written through the same spool robots use."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_runtime.binding import ExecutionBinding
from neura_runtime.capture.episode import Episode, open_episode
from neura_runtime.capture.recorder import new_episode_id
from neura_runtime.capture.spool import Spool


def synthetic_episode(
    directory: Path,
    binding: ExecutionBinding,
    *,
    robot_id: str = "rbt_1",
    revision_id: str = "rev_1",
    steps: int = 8,
    image_stride: int = 1,
    outcome: str = "unknown",
    end: str = "stopped",
    instruction: str = "",
    seed: int = 0,
    actions: NDArray[np.float64] | None = None,
    complete: bool = True,
) -> Episode:
    """A finished episode in ``directory/<episode_id>``: random in-range commands, states and
    frames (or ``actions``, robot order and units)."""
    rng = np.random.default_rng(seed)
    spec = binding.robot.spec
    commands = spec.command_interface
    low = np.array([c.min for c in commands])
    high = np.array([c.max for c in commands])
    if actions is None:
        actions = low + (high - low) * rng.random((steps, len(commands)))
    steps = len(actions)
    roles = {layout.camera_role for layout in binding.image_layouts}
    cameras: list[dict[str, Any]] = [
        {"role": c.role, "width": c.width, "height": c.height, "color_order": c.color_order.value}
        for c in spec.cameras
        if c.role in roles
    ]
    work = directory / ".spool"
    spool = Spool(work, max_bytes=1 << 40)
    episode_id = new_episode_id()
    header: dict[str, Any] = {
        "episode_id": episode_id,
        "robot_id": robot_id,
        "revision_id": revision_id,
        "artifact_digest": binding.artifact_digest,
        "execution_profile_digest": binding.execution_profile_digest,
        "robot_profile_digest": binding.robot_profile_digest,
        "session_id": "ses_test",
        "task_epoch": 1,
        "part": 0,
        "capture_mode": "all",
        "started_at": "2026-10-17T12:00:00.000+00:00",
        "control_period_us": binding.control_period_us,
        "image_stride": image_stride,
        "instruction": instruction,
        "state_names": [f.name for f in spec.state_features],
        "command_names": [c.name for c in commands],
        "cameras": cameras,
    }
    try:
        spool.open(episode_id, header)
        frame = 0
        for i in range(steps):
            index = -1
            if cameras and i % image_stride == 0:
                spool.frames(
                    episode_id,
                    {
                        c["role"]: rng.integers(
                            0, 256, c["width"] * c["height"] * 3, dtype=np.uint8
                        ).tobytes()
                        for c in cameras
                    },
                )
                index = frame
                frame += 1
            state = rng.normal(size=len(spec.state_features))
            spool.step(episode_id, i * binding.control_period_us, i, actions[i], state, index)
        spool.close(
            episode_id,
            {
                "ended_at": "2026-10-17T12:00:10.000+00:00",
                "duration_s": steps * binding.control_period_us / 1e6,
                "end": {"reason": end},
                "outcome": {"label": outcome, "source": "robot", "at": "2026-10-17T12:00:10Z"},
                "complete": complete,
                "events": [],
                "counters": {},
            },
        )
        assert spool.flush()
    finally:
        spool.stop()
    target = directory / episode_id
    shutil.move(str(work / "ready" / episode_id), target)
    shutil.rmtree(work, ignore_errors=True)
    return open_episode(target)
