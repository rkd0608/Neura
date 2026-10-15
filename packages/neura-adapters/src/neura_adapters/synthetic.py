"""Checkpoints with the right architecture and random weights, for tests and benchmarks.

Latency, memory and batching depend on a model's architecture and input shapes, not on what its
weights learned, so benchmarks of serving can run on synthetic checkpoints. Anything measured on
one says so: task success needs trained weights (design §10).

ACT and Diffusion Policy can be built offline; SmolVLA and pi0 start from pretrained
vision-language backbones that are downloaded from their publishers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from neura_contracts import ImageInput, ModelArtifact, StateInput

Size = Literal["tiny", "small", "base"]

# ACT sizes: "base" is LeRobot's default ACT (about 52M parameters, ResNet-18 backbone).
ACT_SIZES: dict[str, dict[str, Any]] = {
    "tiny": {"dim_model": 64, "dim_feedforward": 128, "n_encoder_layers": 1, "n_heads": 4},
    "small": {"dim_model": 256, "dim_feedforward": 1024, "n_encoder_layers": 2, "n_heads": 8},
    "base": {},
}


def write_act_checkpoint(
    artifact: ModelArtifact,
    directory: Path,
    *,
    size: Size = "small",
    seed: int = 0,
    action_ranges: Sequence[tuple[float, float]] | None = None,
) -> Path:
    """Write a LeRobot ACT checkpoint matching the artifact's inputs, outputs and chunk length,
    with its pre- and post-processors. Normalization statistics are neutral for inputs and center
    actions mid-range (within ``action_ranges``, in model units), so outputs stay in range."""
    import torch
    from lerobot.configs.types import FeatureType, PolicyFeature
    from lerobot.policies.act.configuration_act import ACTConfig
    from lerobot.policies.act.modeling_act import ACTPolicy
    from lerobot.policies.factory import make_pre_post_processors

    torch.manual_seed(seed)
    spec = artifact.spec
    inputs: dict[str, PolicyFeature] = {}
    stats: dict[str, dict[str, Any]] = {}
    for model_input in spec.inputs:
        if isinstance(model_input, StateInput):
            dim = len(model_input.components)
            inputs[model_input.name] = PolicyFeature(type=FeatureType.STATE, shape=(dim,))
            stats[model_input.name] = {"mean": torch.zeros(dim), "std": torch.ones(dim) * 50}
        elif isinstance(model_input, ImageInput):
            shape = (3, model_input.height, model_input.width)
            inputs[model_input.name] = PolicyFeature(type=FeatureType.VISUAL, shape=shape)
            stats[model_input.name] = {
                "mean": torch.full((3, 1, 1), 0.5),
                "std": torch.full((3, 1, 1), 0.25),
            }
    action_dim = len(spec.outputs.features)
    stats["action"] = _action_stats(action_dim, action_ranges)
    chunk = spec.outputs.chunk_length
    config = ACTConfig(
        input_features=inputs,
        output_features={"action": PolicyFeature(type=FeatureType.ACTION, shape=(action_dim,))},
        chunk_size=chunk,
        n_action_steps=chunk,
        pretrained_backbone_weights=None,
        device="cpu",
        **ACT_SIZES[size],
    )
    policy = ACTPolicy(config)
    preprocessor, postprocessor = make_pre_post_processors(config, dataset_stats=stats)
    directory.mkdir(parents=True, exist_ok=True)
    policy.save_pretrained(directory)
    preprocessor.save_pretrained(directory)
    postprocessor.save_pretrained(directory)
    return directory


# Diffusion Policy sizes: "base" is LeRobot's default (about 270M parameters, mostly the U-Net).
DIFFUSION_SIZES: dict[str, dict[str, Any]] = {
    "tiny": {"down_dims": (32, 64), "diffusion_step_embed_dim": 32, "num_inference_steps": 5},
    "small": {
        "down_dims": (64, 128, 256),
        "diffusion_step_embed_dim": 64,
        "num_inference_steps": 10,
    },
    "base": {"num_inference_steps": 10},
}


def _features(artifact: ModelArtifact) -> tuple[dict[str, Any], dict[str, dict[str, Any]], int]:
    import torch
    from lerobot.configs.types import FeatureType, PolicyFeature

    inputs: dict[str, Any] = {}
    stats: dict[str, dict[str, Any]] = {}
    for model_input in artifact.spec.inputs:
        if isinstance(model_input, StateInput):
            dim = len(model_input.components)
            inputs[model_input.name] = PolicyFeature(type=FeatureType.STATE, shape=(dim,))
            stats[model_input.name] = {
                "mean": torch.zeros(dim),
                "std": torch.ones(dim) * 50,
                "min": torch.full((dim,), -100.0),
                "max": torch.full((dim,), 100.0),
            }
        elif isinstance(model_input, ImageInput):
            shape = (3, model_input.height, model_input.width)
            inputs[model_input.name] = PolicyFeature(type=FeatureType.VISUAL, shape=shape)
            stats[model_input.name] = {
                "mean": torch.full((3, 1, 1), 0.5),
                "std": torch.full((3, 1, 1), 0.25),
                "min": torch.zeros((3, 1, 1)),
                "max": torch.ones((3, 1, 1)),
            }
    action_dim = len(artifact.spec.outputs.features)
    return inputs, stats, action_dim


def _action_stats(action_dim: int, ranges: Sequence[tuple[float, float]] | None) -> dict[str, Any]:
    """Normalization statistics that keep a random network's actions inside the given ranges:
    centered mid-range, with a spread of a twentieth of each range (and a min-max span of the
    central half, for policies that normalize to [-1, 1])."""
    import torch

    if ranges is None:
        ranges = [(-10.0, 10.0)] * action_dim
    if len(ranges) != action_dim:
        raise ValueError(f"{len(ranges)} action ranges for {action_dim} actions")
    low = torch.tensor([lo for lo, _ in ranges], dtype=torch.float32)
    high = torch.tensor([hi for _, hi in ranges], dtype=torch.float32)
    mid, span = (low + high) / 2, high - low
    return {"mean": mid, "std": span / 20, "min": mid - span / 4, "max": mid + span / 4}


def write_diffusion_checkpoint(
    artifact: ModelArtifact,
    directory: Path,
    *,
    size: Size = "small",
    seed: int = 0,
    action_ranges: Sequence[tuple[float, float]] | None = None,
) -> Path:
    """Write a LeRobot Diffusion Policy checkpoint for the artifact: one observation per
    prediction, the artifact's chunk length as its action steps."""
    import torch
    from lerobot.configs.types import FeatureType, PolicyFeature
    from lerobot.policies.diffusion.configuration_diffusion import DiffusionConfig
    from lerobot.policies.diffusion.modeling_diffusion import DiffusionPolicy
    from lerobot.policies.factory import make_pre_post_processors

    torch.manual_seed(seed)
    inputs, stats, action_dim = _features(artifact)
    stats["action"] = _action_stats(action_dim, action_ranges)
    chunk = artifact.spec.outputs.chunk_length
    horizon = 8 * -(-chunk // 8)  # the U-Net halves the horizon per level: a multiple of 8
    config = DiffusionConfig(
        input_features=inputs,
        output_features={"action": PolicyFeature(type=FeatureType.ACTION, shape=(action_dim,))},
        n_obs_steps=1,
        horizon=horizon,
        n_action_steps=chunk,
        pretrained_backbone_weights=None,
        crop_shape=None,
        device="cpu",
        **DIFFUSION_SIZES[size],
    )
    policy = DiffusionPolicy(config)
    preprocessor, postprocessor = make_pre_post_processors(config, dataset_stats=stats)
    directory.mkdir(parents=True, exist_ok=True)
    policy.save_pretrained(directory)
    preprocessor.save_pretrained(directory)
    postprocessor.save_pretrained(directory)
    return directory


def weights_manifest(directory: Path) -> list[dict[str, Any]]:
    """The artifact's `spec.weights` entries for every file of a checkpoint directory."""
    entries = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        entries.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "digest": f"sha256:{digest.hexdigest()}",
                "size_bytes": path.stat().st_size,
            }
        )
    return entries
