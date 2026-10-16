"""Checkpoints with the right architecture and random weights, for tests and benchmarks.

Latency, memory and batching depend on a model's architecture and input shapes, not on what its
weights learned, so benchmarks of serving can run on synthetic checkpoints. Anything measured on
one says so: task success needs trained weights (design §10).

ACT, Diffusion Policy and pi0 are built offline. SmolVLA reads its vision-language backbone's
configuration and processor from the Hugging Face Hub (not its weights), and so does every
language-conditioned model's tokenizer.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator, Sequence
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


# SmolVLA sizes: "base" is LeRobot's smolvla_base, the first 16 layers of SmolVLM2-500M and an
# action expert three quarters as wide (about 450M parameters).
SMOLVLA_SIZES: dict[str, dict[str, Any]] = {
    "tiny": {"num_vlm_layers": 2},
    "small": {"num_vlm_layers": 8},
    "base": {},
}

# pi0 has one size: LeRobot's pi0, PaliGemma (SigLIP and Gemma 2B) with a Gemma 300M action
# expert, about 3.3B parameters. (LeRobot builds PaliGemma's image projector for Gemma 2B's
# width, so a smaller language model does not fit it.) Building it takes about 14 GB of memory;
# it is kept in bfloat16, as LeRobot runs pi0, about 6.6 GB.

# pi0 tokenizes its instruction with PaliGemma's tokenizer, which is gated on the Hugging Face
# Hub. The cost of serving depends on how many tokens the prompt has, not which: pi0 pads it to a
# fixed length (48), so a public tokenizer gives the model inputs of the same shapes. Synthetic
# pi0 checkpoints use this one, and say so in their pre-processor.
PALIGEMMA_TOKENIZER = "google/paligemma-3b-pt-224"
PUBLIC_TOKENIZER = "HuggingFaceTB/SmolVLM2-500M-Video-Instruct"


def write_smolvla_checkpoint(
    artifact: ModelArtifact,
    directory: Path,
    *,
    size: Size = "small",
    seed: int = 0,
    action_ranges: Sequence[tuple[float, float]] | None = None,
) -> Path:
    """Write a LeRobot SmolVLA checkpoint for the artifact, random weights throughout (its
    backbone's configuration and processor come from the Hub, not its weights)."""
    import torch
    from lerobot.configs.types import FeatureType, PolicyFeature
    from lerobot.policies.factory import make_pre_post_processors
    from lerobot.policies.smolvla.configuration_smolvla import SmolVLAConfig
    from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy

    torch.manual_seed(seed)
    inputs, stats, action_dim = _features(artifact)
    stats["action"] = _action_stats(action_dim, action_ranges)
    chunk = artifact.spec.outputs.chunk_length
    config = SmolVLAConfig(
        input_features=inputs,
        output_features={"action": PolicyFeature(type=FeatureType.ACTION, shape=(action_dim,))},
        chunk_size=chunk,
        n_action_steps=chunk,
        load_vlm_weights=False,
        device="cpu",
        **SMOLVLA_SIZES[size],
    )
    policy = SmolVLAPolicy(config)
    preprocessor, postprocessor = make_pre_post_processors(config, dataset_stats=stats)
    directory.mkdir(parents=True, exist_ok=True)
    policy.save_pretrained(directory)
    preprocessor.save_pretrained(directory)
    postprocessor.save_pretrained(directory)
    return directory


def write_pi0_checkpoint(
    artifact: ModelArtifact,
    directory: Path,
    *,
    size: Size = "small",
    seed: int = 0,
    action_ranges: Sequence[tuple[float, float]] | None = None,
    tokenizer: str = PUBLIC_TOKENIZER,
) -> Path:
    """Write a LeRobot pi0 checkpoint for the artifact, random weights, its pre-processor
    tokenizing with ``tokenizer`` in place of PaliGemma's (see ``PUBLIC_TOKENIZER``). pi0 has one
    size, base: any other is refused."""
    import torch
    from lerobot.configs.types import FeatureType, PolicyFeature
    from lerobot.policies.factory import make_pre_post_processors
    from lerobot.policies.pi0.configuration_pi0 import PI0Config
    from lerobot.policies.pi0.modeling_pi0 import PI0Policy

    if size != "base":
        raise ValueError(f"pi0 has one size, base (LeRobot's pi0), not {size}")
    torch.manual_seed(seed)
    inputs, stats, action_dim = _features(artifact)
    stats["action"] = _action_stats(action_dim, action_ranges)
    chunk = artifact.spec.outputs.chunk_length
    config = PI0Config(
        input_features=inputs,
        output_features={"action": PolicyFeature(type=FeatureType.ACTION, shape=(action_dim,))},
        chunk_size=chunk,
        n_action_steps=chunk,
        dtype="bfloat16",
        device="cpu",
    )
    policy = PI0Policy(config)
    with substituted_tokenizer(PALIGEMMA_TOKENIZER, tokenizer):
        preprocessor, postprocessor = make_pre_post_processors(config, dataset_stats=stats)
    directory.mkdir(parents=True, exist_ok=True)
    policy.save_pretrained(directory)
    preprocessor.save_pretrained(directory)
    postprocessor.save_pretrained(directory)
    rename_tokenizer(directory, PALIGEMMA_TOKENIZER, tokenizer)
    return directory


@contextlib.contextmanager
def substituted_tokenizer(name: str, replacement: str) -> Iterator[None]:
    """While building a pipeline, a tokenizer step asked for ``name`` loads ``replacement``."""
    from lerobot.processor import tokenizer_processor

    auto = tokenizer_processor.AutoTokenizer

    class Substituted:
        @staticmethod
        def from_pretrained(asked: str, *args: Any, **kwargs: Any) -> Any:
            return auto.from_pretrained(replacement if asked == name else asked, *args, **kwargs)

    tokenizer_processor.AutoTokenizer = Substituted
    try:
        yield
    finally:
        tokenizer_processor.AutoTokenizer = auto


def rename_tokenizer(directory: Path, name: str, replacement: str) -> None:
    """Name ``replacement`` wherever a saved pipeline in ``directory`` names tokenizer ``name``."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: replacement if k == "tokenizer_name" and v == name else walk(v)
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    for path in directory.glob("policy_*processor*.json"):
        path.write_text(json.dumps(walk(json.loads(path.read_text())), indent=2), encoding="utf-8")


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
