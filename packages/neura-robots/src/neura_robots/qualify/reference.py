"""The policy as LeRobot runs it on its own: the reference for parity (design §10).

``LeRobotReference`` loads the checkpoint with LeRobot's factory and runs LeRobot's own inference
path (``prepare_observation_for_inference``, the checkpoint's pre- and post-processors,
``predict_action_chunk``) on the raw readings of the arm: joint positions in LeRobot's units and
camera frames as captured. None of Neura's code is on this path: not its tensor encoding,
transport, batching, worker or action mapping. Parity compares what the robot received from the
site with what this computes from the same readings.

``select_action`` is LeRobot's per-step loop (``lerobot-record --policy.path``): predict a chunk
when the queue is empty, then pop one action per control period. It is the local baseline for
the closed-loop episodes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_contracts import ImageInput, LanguageInput, ModelArtifact, RobotProfile, StateInput
from neura_runtime.interfaces import SensorSnapshot


class ReferenceError(RuntimeError):
    """The reference cannot run this artifact (it is not a LeRobot policy in robot units)."""


class LeRobotReference:
    def __init__(
        self,
        artifact: ModelArtifact,
        robot: RobotProfile,
        checkpoint: Path,
        *,
        device: str = "cpu",
    ) -> None:
        import torch
        from lerobot.configs.policies import PreTrainedConfig
        from lerobot.policies.factory import get_policy_class, make_pre_post_processors

        self._torch = torch
        self.artifact = artifact
        config = PreTrainedConfig.from_pretrained(checkpoint)
        config.device = device
        self.device = torch.device(device)
        self.policy = get_policy_class(config.type).from_pretrained(checkpoint, config=config)
        self.policy.eval()
        self.policy.to(self.device)
        overrides = {"device_processor": {"device": device}}
        self.pre, self.post = make_pre_post_processors(
            config,
            pretrained_path=str(checkpoint),
            preprocessor_overrides=overrides,
            postprocessor_overrides=overrides,
        )
        self.policy_type = config.type
        self.chunk_length = artifact.spec.outputs.chunk_length
        inputs = artifact.spec.inputs
        self.states = [i for i in inputs if isinstance(i, StateInput)]
        self.images = [i for i in inputs if isinstance(i, ImageInput)]
        self.language = any(isinstance(i, LanguageInput) for i in inputs)
        units = {f.name: f.unit for f in robot.spec.state_features}
        for state in self.states:
            for component in state.components:
                if units.get(component.name) != component.unit:
                    raise ReferenceError(
                        f"{state.name}: {component.name} is {component.unit} in the artifact but "
                        f"{units.get(component.name)} on the robot; the reference feeds LeRobot "
                        "the robot's own readings"
                    )

    def observation(self, snapshot: SensorSnapshot) -> dict[str, NDArray[Any]]:
        """The raw observation in LeRobot's layout: state in the robot's own units (as LeRobot
        reads and trains on them), images H x W x 3 uint8."""
        out: dict[str, NDArray[Any]] = {}
        for state in self.states:
            try:
                values = [snapshot.state[c.name] for c in state.components]
            except KeyError as exc:
                raise ReferenceError(f"the arm does not report {exc}") from exc
            out[state.name] = np.asarray(values, dtype=np.float32)
        for image in self.images:
            frame = snapshot.images.get(image.camera_role)
            if frame is None:
                raise ReferenceError(f"no {image.camera_role} frame in the snapshot")
            pixels = np.frombuffer(frame.data, dtype=np.uint8)
            out[image.name] = pixels.reshape(frame.height, frame.width, 3).copy()
        return out

    def chunk(self, snapshot: SensorSnapshot, instruction: str = "") -> NDArray[np.float64]:
        """The action chunk LeRobot predicts from these readings, [chunk_length, action_dim]."""
        from lerobot.policies.utils import prepare_observation_for_inference

        torch = self._torch
        with torch.inference_mode():
            batch = prepare_observation_for_inference(
                self.observation(snapshot), self.device, instruction or None
            )
            processed = self.pre(batch)
            if self.policy_type == "diffusion":
                for state in self.states:
                    processed[state.name] = processed[state.name].unsqueeze(1)
            actions = self.post(self.policy.predict_action_chunk(processed))
        chunk: NDArray[np.float64] = np.asarray(actions.detach().to("cpu").numpy(), np.float64)
        return chunk[0, : self.chunk_length]

    def reset(self) -> None:
        self.policy.reset()

    def select_action(self, snapshot: SensorSnapshot, instruction: str = "") -> NDArray[np.float64]:
        """One action, as LeRobot's control loop gets it (``predict_action``)."""
        from lerobot.common.control_utils import predict_action

        action = predict_action(
            self.observation(snapshot),
            self.policy,
            self.device,
            self.pre,
            self.post,
            use_amp=False,
            task=instruction or None,
        )
        return np.asarray(action.detach().to("cpu").numpy(), dtype=np.float64).reshape(-1)
