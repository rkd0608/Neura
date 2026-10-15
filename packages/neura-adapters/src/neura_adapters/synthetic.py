"""Checkpoints with the right architecture and random weights, for tests and benchmarks.

Latency, memory and batching depend on a model's architecture and input shapes, not on what its
weights learned, so benchmarks of serving can run on synthetic checkpoints. Anything measured on
one says so: task success needs trained weights (design §10).

Only ACT can be built offline; SmolVLA and pi0 start from pretrained vision-language backbones that
are downloaded from their publishers.
"""

from __future__ import annotations

import hashlib
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
    artifact: ModelArtifact, directory: Path, *, size: Size = "small", seed: int = 0
) -> Path:
    """Write a LeRobot ACT checkpoint matching the artifact's inputs, outputs and chunk length,
    with its pre- and post-processors. Normalization statistics are neutral for inputs and center
    actions mid-range, so outputs stay in a plausible range."""
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
    stats["action"] = {"mean": torch.zeros(action_dim), "std": torch.ones(action_dim) * 5}
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
