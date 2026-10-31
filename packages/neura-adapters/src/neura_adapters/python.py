"""Policies brought as Python classes: ``@neura.policy`` (design §29.3).

Two ways a class reaches a worker:

- **python**: the version's files are the class's directory (code and weights). Its
  ``neura-policy.json`` names the class (``module:Class``) relative to them.
- **container**: the class is installed in the version's runtime image, an image built from Neura's
  worker image with the customer's package in it. ``neura-policy.json`` names it on the image's
  Python path (``acme_picker.policy:Picker``).

Either way the worker builds the observation the class reads (LeRobot's names:
``observation.state``, ``observation.images.<role>``, ``task``), calls ``act`` (or ``act_batch``
for a whole batch) and checks what comes back: a (chunk_length, action features) array of finite
numbers, in the robot's command order and units. The class runs in the worker sandbox like any
other model code.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from neura_adapters.base import AdapterProperties, blank_inputs
from neura_contracts import ReasonCode, authoring
from neura_contracts.inference import PolicyDeclaration
from neura_runtime.binding import ExecutionBinding
from neura_runtime.policy import ModelInputs, PolicyError


def _entry(checkpoint: Path | None) -> dict[str, Any]:
    if checkpoint is None or not (checkpoint / authoring.ENTRY_FILE).is_file():
        raise PolicyError(
            f"the version has no {authoring.ENTRY_FILE}: push it with `neura policy push`",
            reason=ReasonCode.WORKER_LOAD_FAILED,
        )
    try:
        entry: dict[str, Any] = json.loads(
            (checkpoint / authoring.ENTRY_FILE).read_text(encoding="utf-8")
        )
        PolicyDeclaration.from_json(entry["declaration"])
        str(entry["entry"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PolicyError(
            f"{authoring.ENTRY_FILE} is not valid: {error}", reason=ReasonCode.WORKER_LOAD_FAILED
        ) from error
    return entry


def _installed_class(entry: str) -> type:
    module_name, _, class_name = entry.partition(":")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name, None)
    if not isinstance(cls, type) or authoring.authored(cls) is None:
        raise authoring.AuthoringError(f"{module_name} has no @neura.policy class {class_name}")
    return cls


class PythonPolicy:
    def __init__(
        self,
        *,
        name: str,
        binding: ExecutionBinding,
        checkpoint: Path | None,
        device: str,
        precision: Literal["fp32", "fp16", "bf16"],
    ) -> None:
        entry = _entry(checkpoint)
        assert checkpoint is not None
        declaration = PolicyDeclaration.from_json(entry["declaration"])
        try:
            if name == "container":
                cls = _installed_class(entry["entry"])
                weights = checkpoint
            else:
                cls = authoring.load_class(checkpoint, entry["entry"])
                weights = checkpoint / entry["weights"] if entry.get("weights") else checkpoint
            self._policy = authoring.instantiate(cls, weights=weights, device=device)
        except Exception as error:  # the customer's code: anything can go wrong while loading
            raise PolicyError(
                f"loading {entry['entry']} failed: {type(error).__name__}: {error}",
                out_of_memory=isinstance(error, MemoryError),
                reason=None if isinstance(error, MemoryError) else ReasonCode.WORKER_LOAD_FAILED,
            ) from error
        self.binding = binding
        self.chunk_length = binding.chunk_length
        self.action_dim = binding.action_dim
        self._batch = getattr(self._policy, "act_batch", None)
        self.properties = AdapterProperties(
            name=name,
            deterministic=declaration.deterministic,
            batching=callable(self._batch),
            stateless=not declaration.stateful,
            device=device,
            precision=precision,
            notes=(f"{entry['entry']}",),
        )

    def warmup(self, batch_sizes: Sequence[int] = (1,)) -> None:
        blank = blank_inputs(self.binding)
        for size in batch_sizes:
            self.infer([blank] * size)

    @staticmethod
    def _observation(inputs: ModelInputs) -> dict[str, Any]:
        observation: dict[str, Any] = dict(inputs.state)
        for role, image in inputs.images.items():
            observation[f"observation.images.{role}"] = image
        if inputs.instruction:
            observation["task"] = inputs.instruction
        return observation

    def infer(self, batch: Sequence[ModelInputs]) -> list[NDArray[np.float32]]:
        observations = [self._observation(inputs) for inputs in batch]
        try:
            if callable(self._batch) and len(observations) > 1:
                results = list(self._batch(observations))
            else:
                results = [self._policy.act(o) for o in observations]
        except MemoryError as error:
            raise PolicyError(
                f"the policy ran out of memory: {error}", out_of_memory=True
            ) from error
        except Exception as error:  # the customer's code
            raise PolicyError(f"the policy failed: {type(error).__name__}: {error}") from error
        if len(results) != len(observations):
            raise PolicyError(
                f"act_batch returned {len(results)} chunks for {len(observations)} observations"
            )
        expected = (self.chunk_length, self.action_dim)
        chunks = []
        for result in results:
            chunk = np.asarray(result, dtype=np.float32)
            if chunk.shape != expected:
                raise PolicyError(
                    f"the policy returned {tuple(chunk.shape)}; the robot needs {expected} "
                    "(chunk_length, action features)"
                )
            if not np.isfinite(chunk).all():
                raise PolicyError("the policy returned NaN or infinite actions")
            chunks.append(chunk)
        return chunks


def load(
    *,
    name: str,
    binding: ExecutionBinding,
    checkpoint: Path | None = None,
    device: str = "cpu",
    precision: Literal["fp32", "fp16", "bf16"] = "fp32",
    **_: Any,
) -> PythonPolicy:
    """Registry entry point for the ``python`` and ``container`` adapters."""
    return PythonPolicy(
        name=name, binding=binding, checkpoint=checkpoint, device=device, precision=precision
    )
