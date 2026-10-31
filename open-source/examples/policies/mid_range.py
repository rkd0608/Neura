"""An example @neura.policy: every joint toward the middle of its range, for the SO-101 contracts in
examples/contracts. Replace ``act`` with your model's forward pass.

    neura-arm run examples/robots/so101-arm.yaml --policy examples/policies/mid_range.py
    neura-robot serve examples/policies/mid_range.py      # the same policy, for robots elsewhere
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

import neura_contracts.authoring as neura

# The SO-101 profile's command ranges (examples/contracts/so101.robot.yaml): the middle of each.
MIDDLE = [0.0, 0.0, 0.0, 0.0, 0.0, 50.0]
CHUNK = 100  # actions per chunk, as act-so101.artifact.yaml declares


@neura.policy(chunk_length=CHUNK, cameras={"front": (640, 480), "wrist": (640, 480)})
class MidRange:
    """Every joint toward the middle of its range."""

    def __init__(self, device: str = "cpu") -> None:
        self.device = device  # where a real model would be loaded

    def act(self, observation: dict[str, Any]) -> NDArray[np.float32]:
        # observation["observation.state"]: float32 joint positions, in the robot's order and units
        # observation["observation.images.front"]: uint8 (480, 640, 3), likewise "...wrist"
        return np.tile(np.array(MIDDLE, dtype=np.float32), (CHUNK, 1))
