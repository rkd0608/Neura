"""Test doubles for integrations: an in-memory controller and sensor source.

Shipped with the package so integrators can unit-test their own code against the runtime.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura.v1 import common_pb2, dataplane_pb2
from neura_contracts import FaultResponse
from neura_runtime import tensors
from neura_runtime.binding import ExecutionBinding
from neura_runtime.interfaces import CommandResult, ImageFrame, SensorSnapshot
from neura_runtime.runtime import RobotRuntime, RuntimeConfig, SendObservation


class FakeController:
    def __init__(self) -> None:
        self.authority = True
        self.fail = False
        self.confirm = True
        self.commands: list[NDArray[np.float64]] = []
        self.responses: list[FaultResponse] = []

    def has_authority(self) -> bool:
        return self.authority

    def command(self, action: NDArray[np.float64]) -> CommandResult:
        if self.fail:
            return CommandResult(False, "rejected")
        self.commands.append(action.copy())
        return CommandResult(True)

    def apply_response(self, response: FaultResponse) -> bool:
        self.responses.append(response)
        return self.confirm


class FakeSensors:
    def __init__(self, binding: ExecutionBinding) -> None:
        self.names = [s.name for s in binding.robot.spec.state_features]
        self.cameras = {c.role: (c.width, c.height) for c in binding.robot.spec.cameras}
        self.frames = {role: bytes(w * h * 3) for role, (w, h) in self.cameras.items()}
        self.age_us = 0
        self.camera_lag_us = 0
        self.missing: set[str] = set()
        self.unavailable = False

    def capture(self, now_us: int) -> SensorSnapshot | None:
        if self.unavailable:
            return None
        t = now_us - self.age_us
        return SensorSnapshot(
            state=dict.fromkeys(self.names, 1.0),
            state_time_us=t,
            images={
                role: ImageFrame(w, h, self.frames[role], t - self.camera_lag_us)
                for role, (w, h) in self.cameras.items()
                if role not in self.missing
            },
        )


PERIOD = 33_333
T0 = 5_000_000_000  # robot clock; deliberately unrelated to any other clock


class RuntimeHarness:
    """A runtime on fake hardware, driven one control period at a time."""

    def __init__(self, binding: ExecutionBinding, config: RuntimeConfig | None = None) -> None:
        self.binding = binding
        self.controller = FakeController()
        self.sensors = FakeSensors(binding)
        self.runtime = RobotRuntime(binding, self.controller, self.sensors, config)
        self.now = T0

    def start(self) -> dataplane_pb2.Observation:
        self.runtime.attach_session("ses_a", 7)
        self.runtime.arm(self.now)
        self.runtime.outbox.clear()
        return self.tick_for_observation()

    def tick(self, n: int = 1) -> list[Any]:
        out: list[Any] = []
        for _ in range(n):
            self.now += PERIOD
            self.runtime.tick(self.now)
            out.extend(self.runtime.outbox)
            self.runtime.outbox.clear()
        return out

    def tick_for_observation(self) -> dataplane_pb2.Observation:
        for item in self.tick():
            if isinstance(item, SendObservation):
                return item.message
        raise AssertionError("no observation sent")

    def chunk_for(
        self,
        observation: dataplane_pb2.Observation,
        values: NDArray[np.floating[Any]] | None = None,
        **envelope_overrides: Any,
    ) -> dataplane_pb2.ActionChunk:
        h, d = self.binding.chunk_length, self.binding.action_dim
        if values is None:
            values = np.zeros((h, d), dtype=np.float32)
        actions = tensors.encode(values.astype(np.float32))
        envelope = common_pb2.Envelope()
        envelope.CopyFrom(observation.envelope)
        for key, value in envelope_overrides.items():
            setattr(envelope, key, value)
        return dataplane_pb2.ActionChunk(
            envelope=envelope,
            start_index=observation.anchor_index,
            actions=actions,
            actions_digest=common_pb2.PayloadDigest(sha256=tensors.payload_digest(actions.data)),
        )
