"""What a policy takes and gives, wherever it runs: validated model inputs, the failure a robot
is told about, and the client the robot-side process asks for action chunks (design §30.2).

A ``PolicyClient`` is one way of reaching a policy: in this process (``InProcessPolicy``, for
inference on the robot computer), or a policy server elsewhere. Every client is driven the same
way by ``neura_runtime.local.LocalConnection``, so freshness, task epochs, merging and faults stay
the runtime's whatever the transport.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from neura_contracts import ReasonCode


@dataclass(frozen=True, slots=True)
class ModelInputs:
    """One request's inputs, already validated against the contract (neura_server.inputs)."""

    state: dict[str, NDArray[np.float32]]  # state input name -> components
    images: dict[str, NDArray[np.uint8]]  # camera role -> H x W x 3, in the camera's color order
    instruction: str
    frozen_prefix: NDArray[np.float32] | None


class PolicyError(Exception):
    """Loading or inference failed. ``reason`` is the reason code robots receive for the
    requests of the failed batch; it defaults from ``out_of_memory``."""

    def __init__(
        self, message: str, *, out_of_memory: bool = False, reason: ReasonCode | None = None
    ) -> None:
        super().__init__(message)
        self.out_of_memory = out_of_memory
        if reason is None:
            reason = (
                ReasonCode.WORKER_OUT_OF_MEMORY
                if out_of_memory
                else ReasonCode.WORKER_INFERENCE_FAILED
            )
        self.reason = reason


class PolicyClient(Protocol):
    async def infer(self, inputs: ModelInputs) -> NDArray[np.float32]:
        """One action chunk, (chunk length, action dimension), for one observation's inputs.
        Raises PolicyError when the policy cannot give one."""
        ...

    async def close(self) -> None: ...


class InProcessPolicy:
    """A policy in this process: ``infer`` is called on a worker thread, one request at a time,
    so the control loop keeps its period while the model runs."""

    def __init__(self, infer: Callable[[ModelInputs], NDArray[np.float32]]) -> None:
        self._infer = infer
        self._lock = asyncio.Lock()

    async def infer(self, inputs: ModelInputs) -> NDArray[np.float32]:
        async with self._lock:
            try:
                return await asyncio.to_thread(self._infer, inputs)
            except PolicyError:
                raise
            except Exception as exc:  # the policy's own failure, reported to the runtime
                raise PolicyError(f"{type(exc).__name__}: {exc}") from exc

    async def close(self) -> None:
        return None
