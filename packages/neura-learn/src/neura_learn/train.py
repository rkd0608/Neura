"""Fine-tuning a policy on the site's episodes (design §28.4), with LeRobot's own training loop.

A job starts from a base checkpoint (the artifact the site runs), a LeRobot dataset exported from
a dataset snapshot (``neura_server.capture.export``) and a recipe, and produces a checkpoint in
the same layout, ready to become a new artifact. By default the fine-tune keeps the base model's
normalization: a few hundred recorded episodes should adjust a policy, not move every input it
was trained on (``normalization: "dataset"`` uses the new data's statistics, as LeRobot does).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from neura_adapters.lerobot import policy_config

Progress = Callable[[str], None]
log = logging.getLogger("neura.learn")

# LeRobot's training line. Its step is rounded from 1000 on (1K, 2K), so it is not read.
_LOSS = re.compile(r"\bstep:\S+ .*?\bloss:(?P<loss>[0-9.eE+-]+)")


@dataclass(frozen=True, slots=True)
class Recipe:
    steps: int = 2000
    batch_size: int = 8
    # None: the policy's own training preset (ACT: AdamW at 1e-5).
    learning_rate: float | None = None
    seed: int = 1000
    num_workers: int = 2
    normalization: Literal["base", "dataset"] = "base"
    log_every: int = 50

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Recipe:
        recipe = cls(**data)
        if not 1 <= recipe.steps <= 10_000_000 or not 1 <= recipe.batch_size <= 4096:
            raise ValueError("steps and batch_size must be positive and bounded")
        if recipe.learning_rate is not None and not 0 < recipe.learning_rate < 1:
            raise ValueError("learning_rate must be between 0 and 1")
        if recipe.num_workers < 0 or recipe.log_every < 1:
            raise ValueError("num_workers must be >= 0 and log_every >= 1")
        if recipe.normalization not in ("base", "dataset"):
            raise ValueError("normalization is base or dataset")
        return recipe

    def to_json(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(slots=True)
class TrainingReport:
    checkpoint: Path
    steps: int
    seconds: float
    losses: list[tuple[int, float]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        first = self.losses[0][1] if self.losses else None
        last = self.losses[-1][1] if self.losses else None
        return {
            "steps": self.steps,
            "seconds": round(self.seconds, 1),
            "loss_first": first,
            "loss_last": last,
            "losses": [[step, round(loss, 6)] for step, loss in self.losses[-200:]],
        }


class _LossLog(logging.Handler):
    """LeRobot logs its training metrics as text; keep each logged step's loss. It logs every
    ``log_every``-th step from the first (``step % log_freq == 0``): the k-th line is step
    k * ``log_every``."""

    def __init__(self, progress: Progress, log_every: int) -> None:
        super().__init__(logging.INFO)
        self.losses: list[tuple[int, float]] = []
        self.progress = progress
        self.log_every = log_every

    def emit(self, record: logging.LogRecord) -> None:
        match = _LOSS.search(record.getMessage())
        if match is None:
            return
        step = (len(self.losses) + 1) * self.log_every
        loss = float(match.group("loss"))
        self.losses.append((step, loss))
        self.progress(f"step {step}: loss {loss:.4f}")


def base_statistics(checkpoint: Path) -> dict[str, dict[str, list[Any]]]:
    """The normalization statistics a checkpoint's preprocessor applies, by feature."""
    from lerobot.processor import PolicyProcessorPipeline

    pipeline = PolicyProcessorPipeline.from_pretrained(
        checkpoint, config_filename="policy_preprocessor.json"
    )
    for step in pipeline.steps:
        stats = getattr(step, "stats", None)
        if stats:
            return {
                feature: {name: _plain(value) for name, value in values.items()}
                for feature, values in stats.items()
            }
    return {}


def _plain(value: Any) -> Any:
    tolist = getattr(value, "tolist", None)
    return tolist() if callable(tolist) else value


def keep_base_normalization(dataset_root: Path, checkpoint: Path) -> list[str]:
    """Write the base checkpoint's statistics over the dataset's, for the features the model
    normalizes. Returns those features."""
    stats_path = dataset_root / "meta" / "stats.json"
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    base = base_statistics(checkpoint)
    replaced = []
    for feature, values in base.items():
        if feature in stats:
            stats[feature] = {**stats[feature], **values}
            replaced.append(feature)
    stats_path.write_text(json.dumps(stats), encoding="utf-8")
    return replaced


def fine_tune(
    base: Path,
    dataset_root: Path,
    out: Path,
    recipe: Recipe,
    *,
    device: str = "cpu",
    repo_id: str = "neura/site",
    progress: Progress = lambda _: None,
) -> TrainingReport:
    """Train from ``base`` on the dataset at ``dataset_root``; the new checkpoint is
    ``out/checkpoint``. ``out`` must not exist."""
    from lerobot.configs.default import DatasetConfig
    from lerobot.configs.train import TrainPipelineConfig
    from lerobot.scripts import lerobot_train

    out.mkdir(parents=True)
    if recipe.normalization == "base":
        kept = keep_base_normalization(dataset_root, base)
        progress(f"keeping the base model's normalization for {', '.join(sorted(kept))}")
    policy = policy_config(base)
    policy.pretrained_path = base
    policy.device = device
    policy.push_to_hub = False
    if hasattr(policy, "pretrained_backbone_weights"):
        policy.pretrained_backbone_weights = None  # every weight comes from the base checkpoint
    if recipe.learning_rate is not None and hasattr(policy, "optimizer_lr"):
        policy.optimizer_lr = recipe.learning_rate
    run = out / "run"
    config = TrainPipelineConfig(
        dataset=DatasetConfig(repo_id=repo_id, root=str(dataset_root)),
        policy=policy,
        output_dir=run,
        job_name="neura-fine-tune",
        seed=recipe.seed,
        num_workers=recipe.num_workers,
        batch_size=recipe.batch_size,
        steps=recipe.steps,
        save_checkpoint=True,
        save_freq=recipe.steps,
        log_freq=recipe.log_every,
    )
    handler = _LossLog(progress, recipe.log_every)
    root = logging.getLogger()
    previous_handlers, previous_level = list(root.handlers), root.level
    original = lerobot_train.init_logging

    def init_logging(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)  # LeRobot replaces the root logger's handlers: keep ours
        root.addHandler(handler)

    lerobot_train.init_logging = init_logging
    started = time.monotonic()
    try:
        lerobot_train.train(config)
    finally:
        lerobot_train.init_logging = original
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)
    trained = run / "checkpoints" / "last" / "pretrained_model"
    if not trained.is_dir():
        raise RuntimeError(f"training finished without a checkpoint in {trained.parent}")
    checkpoint = out / "checkpoint"
    shutil.copytree(trained.resolve(), checkpoint)
    (checkpoint / "train_config.json").unlink(missing_ok=True)  # paths of this machine
    shutil.rmtree(run / "checkpoints", ignore_errors=True)
    return TrainingReport(checkpoint, recipe.steps, time.monotonic() - started, handler.losses)
