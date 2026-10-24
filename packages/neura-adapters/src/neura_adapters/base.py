"""The adapter interface policy workers run (design §12).

An adapter loads one model artifact onto one device, warms it up, and runs batches of validated
inputs. It declares the properties the scheduler and the benchmarks rely on. Initial support is
stateless-per-request policies only: an adapter must not carry state from one request to the
next.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from neura_contracts import ImageInput, LanguageInput, ReasonCode, StateInput
from neura_runtime.binding import ExecutionBinding


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


@dataclass(frozen=True, slots=True)
class AdapterProperties:
    """What an adapter declares about its model (design §4.1, §9.1, §12)."""

    name: str
    # Same inputs and seed give the same outputs (parity can then be bit-exact or within a
    # tolerance; stochastic models are compared on distribution statistics).
    deterministic: bool
    # Several requests share one forward pass.
    batching: bool = True
    # One base forward pass can serve requests for different adapters (LoRA) of the model.
    multi_adapter: bool = False
    stateless: bool = True
    device: str = "cpu"
    precision: Literal["fp32", "fp16", "bf16"] = "fp32"
    parameters: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


class PolicyBackend(Protocol):
    """What the gateway and workers call. ``infer`` runs one forward pass for a batch and returns
    one (chunk_length, action_dim) float32 array per input, in the model's declared action order
    and units."""

    chunk_length: int
    action_dim: int

    def infer(self, batch: Sequence[ModelInputs]) -> list[NDArray[np.float32]]: ...


class Adapter(PolicyBackend, Protocol):
    properties: AdapterProperties
    binding: ExecutionBinding

    def warmup(self, batch_sizes: Sequence[int] = (1,)) -> None:
        """Run batches of these sizes so the first real request does not pay for compilation,
        allocation or lazy loading."""
        ...


def blank_inputs(binding: ExecutionBinding) -> ModelInputs:
    """Inputs of the right shapes, for warmup and profiling."""
    spec = binding.artifact.spec
    state: dict[str, NDArray[np.float32]] = {
        i.name: np.zeros(len(i.components), dtype=np.float32)
        for i in spec.inputs
        if isinstance(i, StateInput)
    }
    images: dict[str, NDArray[np.uint8]] = {
        i.camera_role: np.zeros((i.height, i.width, 3), dtype=np.uint8)
        for i in spec.inputs
        if isinstance(i, ImageInput)
    }
    language = any(isinstance(i, LanguageInput) for i in spec.inputs)
    return ModelInputs(
        state=state,
        images=images,
        instruction="pick up the object" if language else "",
        frozen_prefix=None,
    )


AdapterFactory = Callable[..., Adapter]

# name -> "module:function". Real adapters import their heavy dependencies only when loaded.
_REGISTRY: dict[str, str] = {
    "mock": "neura_adapters.mock:load",
    "lerobot-act": "neura_adapters.lerobot:load",
    "lerobot-smolvla": "neura_adapters.lerobot:load",
    "lerobot-pi0": "neura_adapters.lerobot:load",
    "lerobot-pi05": "neura_adapters.lerobot:load",
    "lerobot-diffusion": "neura_adapters.lerobot:load",
    # @neura.policy classes: shipped with the version, or installed in its runtime image.
    "python": "neura_adapters.python:load",
    "container": "neura_adapters.python:load",
}


def available_adapters() -> list[str]:
    return sorted(_REGISTRY)


def load_adapter(
    name: str,
    binding: ExecutionBinding,
    *,
    checkpoint: Path | None = None,
    device: str = "cpu",
    precision: Literal["fp32", "fp16", "bf16"] = "fp32",
    seed: int = 0,
) -> Adapter:
    """Load the adapter an artifact names (``spec.adapter.name``) for its checkpoint."""
    target = _REGISTRY.get(name)
    if target is None:
        raise PolicyError(f"no adapter named {name!r}; available: {available_adapters()}")
    module_name, function = target.split(":")
    module = importlib.import_module(module_name)
    factory: AdapterFactory = getattr(module, function)
    adapter: Adapter = factory(
        name=name,
        binding=binding,
        checkpoint=checkpoint,
        device=device,
        precision=precision,
        seed=seed,
    )
    return adapter
