"""The model-execution interface workers call, plus a deterministic policy for tests and the
simulator. Real adapters (ACT, SmolVLA, π0-class) implement the same interface in M6."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from neura_runtime.binding import ExecutionBinding
from neura_server.inputs import ModelInputs


class PolicyError(Exception):
    """Inference failed. ``out_of_memory`` distinguishes GPU memory exhaustion."""

    def __init__(self, message: str, *, out_of_memory: bool = False) -> None:
        super().__init__(message)
        self.out_of_memory = out_of_memory


class PolicyBackend(Protocol):
    chunk_length: int
    action_dim: int

    def infer(self, batch: Sequence[ModelInputs]) -> list[NDArray[np.float32]]:
        """One forward pass for a batch; returns one (chunk_length, action_dim) array per input,
        in the model's declared action order and units."""
        ...


class MockPolicy:
    """Moves each action toward a fixed target pose over the chunk.

    Deterministic, stateless and in range for the example robot profiles. Each action feature is
    driven from the state component with the same name when one exists.
    """

    def __init__(
        self, binding: ExecutionBinding, target: dict[str, float] | None = None, gain: float = 1.0
    ) -> None:
        self.chunk_length = binding.chunk_length
        self.action_dim = binding.action_dim
        features = [f.name for f in binding.artifact.spec.outputs.features]
        commands = {c.name: c for c in binding.robot.spec.command_interface}
        transform = binding.action_transform
        robot_target = np.array(
            [
                (target or {}).get(name, (commands[name].min + commands[name].max) / 2)
                for name in [c.name for c in binding.robot.spec.command_interface]
            ],
            dtype=np.float64,
        )
        # Express the target in model space so outputs need the profile's transforms applied.
        self._target = transform.to_model(robot_target[None, :])[0]
        self._gain = gain
        self._state_index: list[tuple[str, int] | None] = []
        for name in features:
            found: tuple[str, int] | None = None
            for layout in binding.state_layouts:
                if name in layout.feature_names:
                    found = (layout.input_name, layout.feature_names.index(name))
                    break
            self._state_index.append(found)
        self._fraction = np.minimum(
            1.0, gain * np.arange(1, self.chunk_length + 1, dtype=np.float64) / self.chunk_length
        )[:, None]

    def infer(self, batch: Sequence[ModelInputs]) -> list[NDArray[np.float32]]:
        outputs = []
        for inputs in batch:
            current = np.array(
                [
                    float(inputs.state[loc[0]][loc[1]]) if loc is not None else self._target[i]
                    for i, loc in enumerate(self._state_index)
                ],
                dtype=np.float64,
            )
            chunk = current + (self._target - current) * self._fraction
            outputs.append(chunk.astype(np.float32))
        return outputs
