"""A LeRobot checkpoint, run the way ``lerobot-eval`` runs it (LeRobot 0.6): the environment's
observation through ``preprocess_observation``, the checkpoint's own preprocessor,
``select_action`` (which keeps the policy's action queue) and its postprocessor.

``checkpoint_path`` resolves ``hub:<repo>`` to a local snapshot of a Hugging Face Hub repository.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray


def checkpoint_path(spec: str | Path, workdir: Path) -> tuple[Path, bool]:
    """(local checkpoint, migrated). A checkpoint saved before LeRobot 0.4 has its normalization
    inside the model and no processor files: it is migrated into ``workdir`` with LeRobot's own
    tool (``lerobot.processor.migrate_policy_normalization``), which changes its files."""
    text = str(spec)
    if text.startswith("hub:"):
        from huggingface_hub import snapshot_download

        path = Path(snapshot_download(repo_id=text.removeprefix("hub:")))
    else:
        path = Path(text)
    if (path / "policy_preprocessor.json").is_file():
        return path, False
    migrated = workdir / "migrated"
    subprocess.run(  # noqa: S603 - LeRobot's own migration, on a checkpoint we were given
        [
            sys.executable,
            "-m",
            "lerobot.processor.migrate_policy_normalization",
            "--pretrained-path",
            str(path),
            "--output-dir",
            str(migrated),
        ],
        check=True,
    )
    return migrated, True


class LocalPolicy:
    def __init__(self, checkpoint: Path, device: str = "cpu") -> None:
        import torch
        from lerobot.configs.policies import PreTrainedConfig
        from lerobot.policies.factory import get_policy_class, make_pre_post_processors

        self._torch = torch
        config = PreTrainedConfig.from_pretrained(checkpoint)
        config.device = device
        self.config = config
        policy = get_policy_class(config.type).from_pretrained(checkpoint, config=config)
        policy.eval()
        policy.to(device)
        self.policy = policy
        overrides = {"device_processor": {"device": device}}
        self.pre, self.post = make_pre_post_processors(
            config,
            pretrained_path=str(checkpoint),
            preprocessor_overrides=overrides,
            postprocessor_overrides=overrides,
        )

    def reset(self) -> None:
        self.policy.reset()

    def _observation(self, image: NDArray[np.uint8], state: NDArray[np.float64]) -> Any:
        from lerobot.envs.utils import preprocess_observation

        observation = preprocess_observation(
            {"pixels": {"top": image[None]}, "agent_pos": state[None]}
        )
        observation["task"] = [""]
        return self.pre(observation)

    def select_action(self, image: NDArray[np.uint8], state: NDArray[np.float64]) -> Any:
        """One action, as ``lerobot-eval`` takes it: from the queue, which the policy refills
        with a new chunk (inference) when it is empty."""
        with self._torch.inference_mode():
            action = self.policy.select_action(self._observation(image, state))
        action = self.post(action)
        result: NDArray[np.float32] = action.to("cpu").numpy()[0]
        return result

    def predict_chunk(self, image: NDArray[np.uint8], state: NDArray[np.float64]) -> Any:
        """A whole chunk for one observation (the reference for parity)."""
        with self._torch.inference_mode():
            chunk = self.policy.predict_action_chunk(self._observation(image, state))
        result: NDArray[np.float32] = self.post(chunk).to("cpu").numpy()[0]
        return result

    @property
    def reads_every_observation(self) -> bool:
        """ACT with temporal ensembling runs the model every step; otherwise an observation is
        read only when the queue needs a new chunk."""
        return getattr(self.config, "temporal_ensemble_coeff", None) is not None

    @property
    def queue_empty(self) -> bool:
        """Whether the next ``select_action`` runs the model (the queue needs a new chunk)."""
        queue = getattr(self.policy, "_action_queue", None)
        return queue is None or len(queue) == 0
