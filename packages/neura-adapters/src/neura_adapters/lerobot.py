"""LeRobot policies (ACT, SmolVLA, pi0, pi0.5, Diffusion Policy) as Neura adapters (§2.2, §12).

The checkpoint is a LeRobot policy directory as `save_pretrained` writes it: `config.json`,
`model.safetensors`, and the policy's pre- and post-processor pipelines (normalization and its
statistics). The adapter runs the checkpoint's own processors, so outputs match LeRobot's own
inference for the same inputs.

Loading checks the checkpoint against the contract (input names and shapes, action size, chunk
length) and refuses a mismatch with a clear message: a wrong checkpoint fails at prepare, never on
a robot.

Requires the `lerobot` extra (PyTorch and LeRobot).
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from neura_adapters.base import AdapterProperties, ModelInputs, PolicyError, blank_inputs
from neura_contracts import ImageInput, LanguageInput, ReasonCode, StateInput
from neura_runtime.binding import ExecutionBinding

log = logging.getLogger("neura.adapters.lerobot")

# Adapter name -> LeRobot policy type (config.json "type").
POLICY_TYPES = {
    "lerobot-act": "act",
    "lerobot-smolvla": "smolvla",
    "lerobot-pi0": "pi0",
    "lerobot-pi05": "pi05",
    "lerobot-diffusion": "diffusion",
}
# Policies whose inference is deterministic given their inputs (ACT samples no noise at eval).
DETERMINISTIC = frozenset({"act"})
LANGUAGE_KEY = "task"


_SCRATCH = threading.Lock()


def policy_config(checkpoint: Path | str) -> Any:
    """A LeRobot checkpoint's policy configuration (``PreTrainedConfig.from_pretrained``), without
    the file LeRobot leaves behind.

    LeRobot 0.6.1 writes the configuration to a temporary file it never deletes: one file in the
    system's temporary directory for every model loaded, so every policy worker start at a site.
    It is loaded here with a temporary directory of its own, removed afterwards. Every Neura
    caller loads configurations through this function.
    """
    from lerobot.configs.policies import PreTrainedConfig

    with _SCRATCH, tempfile.TemporaryDirectory(prefix="neura-lerobot-") as scratch:
        previous, tempfile.tempdir = tempfile.tempdir, scratch
        try:
            return PreTrainedConfig.from_pretrained(checkpoint)
        finally:
            tempfile.tempdir = previous


def _torch() -> Any:
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - depends on the installation
        raise PolicyError(
            "LeRobot adapters need PyTorch and LeRobot: install neura-adapters[lerobot]"
        ) from exc
    return torch


def _dtype(precision: str) -> Any:
    torch = _torch()
    return {"fp32": torch.float32, "fp16": torch.float16, "bf16": torch.bfloat16}[precision]


class LeRobotAdapter:
    """One LeRobot policy on one device. Not thread-safe: a worker runs one batch at a time."""

    def __init__(
        self,
        *,
        name: str,
        binding: ExecutionBinding,
        checkpoint: Path,
        device: str = "cpu",
        precision: Literal["fp32", "fp16", "bf16"] = "fp32",
        seed: int = 0,
    ) -> None:
        torch = _torch()
        from lerobot.policies.factory import get_policy_class, make_pre_post_processors

        expected_type = POLICY_TYPES.get(name)
        if expected_type is None:
            raise PolicyError(f"{name!r} is not a LeRobot adapter")
        if not (checkpoint / "config.json").is_file():
            raise PolicyError(f"{checkpoint} is not a LeRobot policy checkpoint (no config.json)")
        config = policy_config(checkpoint)
        if config.type != expected_type:
            raise PolicyError(
                f"the checkpoint is a {config.type!r} policy but the artifact names {name!r}"
            )
        config.device = device
        # The checkpoint holds every weight, its image backbone's included. Building the policy
        # must not first fetch pretrained backbone weights (ImageNet's, from the internet) only to
        # overwrite them: a policy worker has no network (design §12).
        if getattr(config, "pretrained_backbone_weights", None) is not None:
            config.pretrained_backbone_weights = None
        self._check_contract(config, binding)
        # Policies that read a history of observations (Diffusion Policy with n_obs_steps > 1)
        # carry state between requests, which Neura does not serve yet (design §12).
        self._obs_steps = int(getattr(config, "n_obs_steps", 1) or 1)
        if self._obs_steps != 1:
            raise PolicyError(
                f"the checkpoint reads {self._obs_steps} past observations per prediction; only "
                "single-observation (stateless) policies are supported"
            )
        self._time_axis = config.type == "diffusion"  # expects (batch, time, ...) state inputs

        self.binding = binding
        self.chunk_length = binding.chunk_length
        self.action_dim = binding.action_dim
        self._device = torch.device(device)
        self._dtype = _dtype(precision)
        self._seed = seed
        self._generator = torch.Generator(device="cpu").manual_seed(seed)
        policy_class = get_policy_class(config.type)
        policy = policy_class.from_pretrained(checkpoint, config=config)
        policy.eval()
        policy.to(self._device)
        if self._dtype != torch.float32:
            policy.to(self._dtype)
        self.policy = policy
        overrides = {"device_processor": {"device": device}}
        self._pre, self._post = make_pre_post_processors(
            config,
            pretrained_path=str(checkpoint),
            preprocessor_overrides=overrides,
            postprocessor_overrides=overrides,
        )
        self._state = [i for i in binding.artifact.spec.inputs if isinstance(i, StateInput)]
        self._images = [i for i in binding.artifact.spec.inputs if isinstance(i, ImageInput)]
        self._language = next(
            (i for i in binding.artifact.spec.inputs if isinstance(i, LanguageInput)), None
        )
        parameters = sum(int(p.numel()) for p in policy.parameters())
        self.properties = AdapterProperties(
            name=name,
            deterministic=config.type in DETERMINISTIC,
            batching=True,
            multi_adapter=False,
            device=device,
            precision=precision,
            parameters=parameters,
            notes=(f"LeRobot {config.type} policy",),
        )

    # --- contract ---------------------------------------------------------------------------

    @staticmethod
    def _check_contract(config: Any, binding: ExecutionBinding) -> None:
        problems: list[str] = []
        features = config.input_features
        for model_input in binding.artifact.spec.inputs:
            if isinstance(model_input, LanguageInput):
                continue
            feature = features.get(model_input.name)
            if feature is None:
                problems.append(f"input {model_input.name!r} is not an input of the checkpoint")
                continue
            if isinstance(model_input, StateInput):
                expected: tuple[int, ...] = (len(model_input.components),)
            else:
                expected = (3, model_input.height, model_input.width)
            if tuple(feature.shape) != expected:
                problems.append(
                    f"input {model_input.name!r} has shape {tuple(feature.shape)} in the "
                    f"checkpoint but {expected} in the artifact"
                )
        declared = {i.name for i in binding.artifact.spec.inputs}
        for key in features:
            if key not in declared:
                problems.append(f"the checkpoint needs input {key!r}, which the artifact lacks")
        action = config.output_features.get("action")
        if action is None or tuple(action.shape) != (binding.action_dim,):
            shape = None if action is None else tuple(action.shape)
            problems.append(f"the checkpoint's action shape {shape} is not ({binding.action_dim},)")
        # Diffusion Policy returns its n_action_steps; the others their whole chunk.
        chunk = getattr(
            config, "n_action_steps" if config.type == "diffusion" else "chunk_size", None
        )
        if chunk is not None and chunk < binding.chunk_length:
            problems.append(
                f"the checkpoint predicts {chunk} actions but the artifact declares "
                f"{binding.chunk_length}"
            )
        if problems:
            raise PolicyError("checkpoint does not match the artifact: " + "; ".join(problems))

    # --- inference --------------------------------------------------------------------------

    def _batch(self, batch: Sequence[ModelInputs]) -> dict[str, Any]:
        torch = _torch()
        tensors: dict[str, Any] = {}
        for state in self._state:
            stacked = np.stack([inputs.state[state.name] for inputs in batch]).astype(np.float32)
            tensors[state.name] = torch.from_numpy(stacked)
        for image in self._images:
            # H x W x 3 uint8 -> 3 x H x W float in [0, 1], as LeRobot datasets provide.
            stacked_images = np.stack([inputs.images[image.camera_role] for inputs in batch])
            frames = torch.from_numpy(stacked_images).permute(0, 3, 1, 2).float().div_(255.0)
            tensors[image.name] = frames
        if self._language is not None:
            tensors[LANGUAGE_KEY] = [inputs.instruction for inputs in batch]
        return tensors

    def infer(self, batch: Sequence[ModelInputs]) -> list[NDArray[np.float32]]:
        torch = _torch()
        if any(inputs.frozen_prefix is not None for inputs in batch):
            raise PolicyError("this adapter does not support real-time chunking (rtc) yet")
        try:
            # Reduced precision runs under autocast too: the policies create float32 tensors of
            # their own (latents, positional embeddings) that would not match bf16/fp16 weights.
            autocast = (
                torch.autocast(device_type=self._device.type, dtype=self._dtype)
                if self._dtype != torch.float32
                else contextlib.nullcontext()
            )
            with torch.inference_mode(), autocast:
                processed = self._pre(self._batch(batch))
                for key, value in processed.items():
                    if isinstance(value, torch.Tensor) and value.is_floating_point():
                        processed[key] = value.to(self._dtype)
                if self._time_axis:
                    for state in self._state:
                        processed[state.name] = processed[state.name].unsqueeze(1)
                # Stochastic policies draw their noise from a seeded generator, so a batch's
                # outputs are reproducible for the same inputs and seed.
                torch.manual_seed(int(torch.randint(0, 2**31 - 1, (1,), generator=self._generator)))
                actions = self.policy.predict_action_chunk(processed)
                actions = self._post(actions.float())
        except torch.cuda.OutOfMemoryError as exc:
            raise PolicyError(str(exc), out_of_memory=True) from exc
        chunks = actions.detach().to("cpu").numpy().astype(np.float32)
        if chunks.ndim != 3 or chunks.shape[0] != len(batch) or chunks.shape[2] != self.action_dim:
            raise PolicyError(f"the policy returned actions of shape {chunks.shape}")
        return [chunk[: self.chunk_length] for chunk in chunks]

    def warmup(self, batch_sizes: Sequence[int] = (1,)) -> None:
        """Run zero-valued batches so allocation and lazy initialization happen before serving."""
        for size in batch_sizes:
            self.infer([self.blank_inputs()] * size)
        log.info("warmed up %s on %s", self.properties.name, self._device)

    def blank_inputs(self) -> ModelInputs:
        return blank_inputs(self.binding)


def load(
    *,
    name: str,
    binding: ExecutionBinding,
    checkpoint: Path | None,
    device: str = "cpu",
    precision: Literal["fp32", "fp16", "bf16"] = "fp32",
    seed: int = 0,
) -> LeRobotAdapter:
    """Registry entry point."""
    if checkpoint is None:
        raise PolicyError(
            f"{name} needs the artifact's checkpoint directory",
            reason=ReasonCode.AGENT_ARTIFACT_UNAVAILABLE,
        )
    return LeRobotAdapter(
        name=name,
        binding=binding,
        checkpoint=checkpoint,
        device=device,
        precision=precision,
        seed=seed,
    )
