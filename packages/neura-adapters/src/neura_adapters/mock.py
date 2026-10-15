"""A deterministic policy for tests, the simulator and development stacks."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_adapters.base import AdapterProperties, ModelInputs
from neura_runtime.binding import ExecutionBinding


class MockPolicy:
    """Moves each action toward a fixed target pose over the chunk.

    Deterministic, stateless and in range for the example robot profiles. Each action feature is
    driven from the state component with the same name when one exists.
    """

    def __init__(
        self, binding: ExecutionBinding, target: dict[str, float] | None = None, gain: float = 1.0
    ) -> None:
        self.binding = binding
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
        self.properties = AdapterProperties(name="mock", deterministic=True, batching=True)
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

    def warmup(self, batch_sizes: Sequence[int] = (1,)) -> None:
        """Nothing to warm up."""

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


def load(*, binding: ExecutionBinding, checkpoint: Path | None = None, **_: Any) -> MockPolicy:
    """Registry entry point; the mock has no weights."""
    del checkpoint
    return MockPolicy(binding)
