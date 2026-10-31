"""A policy behind LeRobot's async inference server (``lerobot.async_inference.policy_server``,
LeRobot 0.6): the robot's ``PolicyClient`` for it, unchanged on the server's side (design §30.2).

LeRobot's protocol, followed as its own robot client follows it: ``Ready``, then
``SendPolicyInstructions`` telling the server which checkpoint to load and the robot's features;
per request, the observation streamed to ``SendObservations`` and its chunk taken from
``GetActions``. Every observation is sent as one that must be processed, since Neura asks only when
it needs a chunk (the server would otherwise skip one too like the last), and each answer's first
timestep must be the request's, or it answers another one.

Both directions are pickled Python objects, and unpickling runs code: a reply is unpickled only
as LeRobot's ``TimedAction`` holding tensors, and the tensors' storage is loaded with torch's
weights-only loader, where pickle would call ``torch.load`` unrestricted. The server is still
trusted with the robot's observations and with choosing its actions: point the robot only at a
server its owner runs. An empty reply is LeRobot's answer for any failure (its log says which).
Needs ``neura-runtime[lerobot]``.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import pickle
import time
from pathlib import Path
from typing import Any

import grpc
import numpy as np
from numpy.typing import NDArray

from neura_contracts import ReasonCode
from neura_runtime.binding import ExecutionBinding
from neura_runtime.policy import ModelInputs, PolicyError

_MAX_MESSAGE_BYTES = 64 * 1024 * 1024


def _load_storage(data: bytes) -> Any:
    import torch

    return torch.load(io.BytesIO(data), weights_only=True)


class _ActionsUnpickler(pickle.Unpickler):
    """Unpickles a chunk of LeRobot ``TimedAction``s and nothing else."""

    def find_class(self, module: str, name: str) -> Any:
        if (module, name) == ("torch.storage", "_load_from_bytes"):
            return _load_storage  # pickle's own would torch.load anything, unrestricted
        if (module, name) in {
            ("lerobot.async_inference.helpers", "TimedAction"),
            ("torch._utils", "_rebuild_tensor_v2"),
            ("collections", "OrderedDict"),
        }:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"a LeRobot reply may not contain {module}.{name}")


def unpickle_actions(data: bytes) -> list[Any]:
    actions = _ActionsUnpickler(io.BytesIO(data)).load()
    if not isinstance(actions, list):
        raise pickle.UnpicklingError(f"a LeRobot reply is a list of actions, not {type(actions)}")
    return actions


class LeRobotPolicy:
    """A ``PolicyClient`` for LeRobot's policy server at ``target`` (host:port), serving the
    checkpoint ``checkpoint`` (a path or Hub name the server can load) of ``policy_type`` (act,
    smolvla, pi0, ...) on its ``device``."""

    def __init__(
        self,
        target: str,
        binding: ExecutionBinding,
        *,
        policy_type: str,
        checkpoint: str | Path,
        device: str = "cpu",
        timeout_s: float = 2.0,
        load_timeout_s: float = 300.0,
    ) -> None:
        self.target = target
        self.binding = binding
        self.policy_type = policy_type
        self.checkpoint = str(checkpoint)
        self.device = device
        self._timeout_s = timeout_s
        self._load_timeout_s = load_timeout_s
        options = [
            ("grpc.max_receive_message_length", _MAX_MESSAGE_BYTES),
            ("grpc.max_send_message_length", _MAX_MESSAGE_BYTES),
        ]
        self._channel = grpc.aio.insecure_channel(target, options=options)
        self._stub: Any = None
        self._timesteps = itertools.count(1)
        state = binding.state_layouts
        if len(state) != 1:
            raise PolicyError(
                "LeRobot's server takes one state vector; these contracts have "
                f"{len(state)} state inputs",
                reason=ReasonCode.WORKER_LOAD_FAILED,
            )
        self._state_input = state[0].input_name
        self._joints = list(state[0].feature_names)

    def _features(self) -> dict[str, Any]:
        from lerobot.utils.feature_utils import hw_to_dataset_features

        hardware: dict[str, Any] = dict.fromkeys(self._joints, float)
        for camera in self.binding.robot.spec.cameras:
            hardware[camera.role] = (camera.height, camera.width, 3)
        return hw_to_dataset_features(hardware, "observation", use_video=False)  # type: ignore[no-any-return]

    async def load(self) -> None:
        """Tell the server which policy to serve and wait until it has loaded it."""
        from lerobot.async_inference.helpers import RemotePolicyConfig
        from lerobot.transport import services_pb2, services_pb2_grpc

        self._stub = services_pb2_grpc.AsyncInferenceStub(self._channel)
        config = RemotePolicyConfig(
            policy_type=self.policy_type,
            pretrained_name_or_path=self.checkpoint,
            lerobot_features=self._features(),
            actions_per_chunk=self.binding.chunk_length,
            device=self.device,
        )
        try:
            await self._stub.Ready(services_pb2.Empty(), timeout=self._timeout_s)
            await self._stub.SendPolicyInstructions(
                services_pb2.PolicySetup(data=pickle.dumps(config)),
                timeout=self._load_timeout_s,
            )
        except grpc.aio.AioRpcError as error:
            raise PolicyError(
                f"LeRobot's server at {self.target} did not load {self.checkpoint}: "
                f"{error.code().name} {error.details()}",
                reason=ReasonCode.WORKER_LOAD_FAILED,
            ) from error

    async def infer(self, inputs: ModelInputs) -> NDArray[np.float32]:
        from lerobot.async_inference.helpers import TimedObservation
        from lerobot.transport import services_pb2
        from lerobot.transport.utils import send_bytes_in_chunks

        if self._stub is None:
            await self.load()
        timestep = next(self._timesteps)
        raw: dict[str, Any] = dict(
            zip(self._joints, (float(v) for v in inputs.state[self._state_input]), strict=True)
        )
        raw.update(inputs.images)
        raw["task"] = inputs.instruction
        observation = TimedObservation(
            timestamp=time.time(), timestep=timestep, observation=raw, must_go=True
        )
        data = pickle.dumps(observation)
        try:
            await self._stub.SendObservations(
                send_bytes_in_chunks(data, services_pb2.Observation), timeout=self._timeout_s
            )
            reply = await self._stub.GetActions(services_pb2.Empty(), timeout=self._timeout_s)
        except grpc.aio.AioRpcError as error:
            raise PolicyError(
                f"LeRobot's server at {self.target}: {error.code().name} {error.details()}",
                reason=ReasonCode.WORKER_UNAVAILABLE,
            ) from error
        if not reply.data:
            raise PolicyError(
                f"LeRobot's server at {self.target} returned no actions (see its log)"
            )
        try:
            actions = await asyncio.to_thread(unpickle_actions, reply.data)
        except (pickle.UnpicklingError, AttributeError, TypeError, ValueError) as error:
            raise PolicyError(
                f"LeRobot's server sent something other than actions: {error}",
                reason=ReasonCode.WORKER_OUTPUT_INVALID,
            ) from error
        if not actions or actions[0].timestep != timestep:
            raise PolicyError(
                "LeRobot's server answered another request", reason=ReasonCode.WORKER_OUTPUT_INVALID
            )
        return np.stack([a.action.numpy() for a in actions]).astype(np.float32)

    async def close(self) -> None:
        await self._channel.close()
