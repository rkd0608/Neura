"""An example @neura.policy for the simulated Franka Panda: the arm waves from its home pose and
the hand opens and closes, on a four-second cycle. It reads only the state, so no contracts are
written for it: on a simulated arm they are inferred from the arm's model and this declaration.
Replace ``act`` with your model's forward pass.

    neura-arm run examples/robots/panda-sim.yaml --policy examples/policies/panda_wave.py
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
from numpy.typing import NDArray

import neura_contracts.authoring as neura

CHUNK = 25  # actions per chunk: half a second at the simulated arms' 50 Hz
PERIOD_S = 0.02  # one control period of the simulated arms
CYCLE_S = 4.0
# The Panda's home pose (Menagerie's "home" keyframe): seven joints in rad, then the hand
# (actuator8) as a fraction of its range, 1 open.
HOME = np.array([0.0, 0.0, 0.0, -1.5708, 0.0, 1.5708, -0.7853, 1.0], dtype=np.float32)
WAVE = np.array([0.6, 0.3, 0.0, 0.4, 0.0, -0.3, 0.8, -1.0], dtype=np.float32)


@neura.policy(chunk_length=CHUNK, cameras={})
class PandaWave:
    """The Panda waves from its home pose; the hand opens and closes."""

    def act(self, observation: dict[str, Any]) -> NDArray[np.float32]:
        # observation["observation.state"]: where the arm is, in the profile's order and units
        times = time.time() + PERIOD_S * np.arange(CHUNK)
        phase = ((1 - np.cos(2 * np.pi * times / CYCLE_S)) / 2).astype(np.float32)  # 0 -> 1 -> 0
        chunk: NDArray[np.float32] = HOME + phase[:, None] * WAVE[None, :]
        return chunk
