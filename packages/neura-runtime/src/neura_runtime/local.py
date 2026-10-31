"""The gateway's part for one robot, in the robot-side process: sessions, heartbeats and epochs
answered locally, and each observation served by a ``PolicyClient`` (design §30.2).

This is how the runtime runs without a site gateway: in front of a policy in this process, or of
a policy server it reaches over the network. The runtime and its session client are the ones a
gateway connection drives, so the rules that keep the robot safe (freshness, task epochs, chunk
merging, faults, operator authority) are the same code whatever serves the policy. One request
is in flight at a time: a newer observation supersedes it, a new task epoch fences it, and an
answer that comes too late to be accepted is never delivered.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

from neura.v1 import common_pb2, dataplane_pb2
from neura_contracts import ReasonCode
from neura_runtime.grpc_client import ConnectionLoop, monotonic_us
from neura_runtime.policy import ModelInputs, PolicyClient, PolicyError
from neura_runtime.serving import InvalidObservation, chunk_from, decode_observation
from neura_runtime.session import Outbound, SessionClient

log = logging.getLogger("neura.local")

LEASE_TTL_US = 2_000_000


class LocalConnection(ConnectionLoop):
    def __init__(
        self,
        client: SessionClient,
        policy: PolicyClient,
        *,
        on_tick: Callable[[int], None] | None = None,
        clock: Callable[[], int] = monotonic_us,
    ) -> None:
        super().__init__(client, on_tick, clock)
        self.policy = policy
        self.counters: Counter[str] = Counter()
        self._generation = 0
        self._session_id = ""
        self._epoch = 0
        self._serving: asyncio.Task[None] | None = None
        self._serving_epoch = 0

    async def run(self, stop: asyncio.Event) -> None:
        try:
            await self._control_loop(stop, self._dispatch)
        finally:
            self._cancel()
            self.client.close()

    # --- the robot's requests, answered as the gateway would

    def _dispatch(self, item: Outbound) -> None:
        message = item.message
        if item.channel == "open":
            self._cancel()
            self._generation += 1
            self._session_id = f"local-{self._generation}"
            self._epoch = 0
            accepted = dataplane_pb2.SessionAccepted(
                session_id=self._session_id,
                lease_generation=self._generation,
                lease_ttl_us=LEASE_TTL_US,
            )
            self._reply(
                self.client.on_open_response, dataplane_pb2.OpenSessionResponse(accepted=accepted)
            )
        elif item.channel == "heartbeat":
            assert isinstance(message, dataplane_pb2.HeartbeatRequest)
            if not self._current(message.session_id, message.lease_generation):
                rejected = _reason(ReasonCode.SESSION_UNKNOWN)
                response = dataplane_pb2.HeartbeatResponse(rejected=rejected)
            else:
                ack = dataplane_pb2.HeartbeatAck(
                    robot_send_time_us=message.robot_send_time_us, lease_remaining_us=LEASE_TTL_US
                )
                response = dataplane_pb2.HeartbeatResponse(ack=ack)
            self._reply(self.client.on_heartbeat_response, response)
        elif item.channel == "control":
            assert isinstance(message, dataplane_pb2.ControlRequest)
            self._reply(self.client.on_control_response, self._control(message))
        else:
            assert isinstance(message, dataplane_pb2.RobotMessage)
            if message.WhichOneof("body") == "observation":
                self._observe(message.observation)

    def _control(self, request: dataplane_pb2.ControlRequest) -> dataplane_pb2.ControlResponse:
        env = request.envelope
        if not self._current(env.session_id, env.lease_generation):
            return dataplane_pb2.ControlResponse(rejected=_reason(ReasonCode.SESSION_UNKNOWN))
        command = request.WhichOneof("command")
        if command == "epoch_advanced":
            self._epoch = max(self._epoch, env.task_epoch)
            if self._serving_epoch < self._epoch:
                self._cancel()  # fenced: nothing from an earlier task reaches the robot
        elif command == "invalidate":
            self._cancel()
        elif command == "close":
            self._cancel()
            self._session_id = ""
        else:
            return dataplane_pb2.ControlResponse(
                rejected=_reason(ReasonCode.PROTOCOL_MESSAGE_INVALID, "no command")
            )
        return dataplane_pb2.ControlResponse(ack=dataplane_pb2.ControlAck(task_epoch=self._epoch))

    def _observe(self, observation: dataplane_pb2.Observation) -> None:
        env = observation.envelope
        if env.protocol_version != common_pb2.PROTOCOL_VERSION_1:
            self._reject(env, ReasonCode.PROTOCOL_VERSION_MISMATCH)
            return
        if not self._current(env.session_id, env.lease_generation):
            self._reject(env, ReasonCode.SESSION_UNKNOWN)
            return
        if env.task_epoch < self._epoch:
            self._reject(env, ReasonCode.SESSION_EPOCH_STALE)
            return
        decoded = decode_observation(observation, self.client.runtime.binding)
        if isinstance(decoded, InvalidObservation):
            self._reject(env, decoded.reason, decoded.detail)
            return
        if self._serving is not None and not self._serving.done():
            self._serving.cancel()
            self.counters[ReasonCode.REQUEST_SUPERSEDED.value] += 1
        self._epoch = env.task_epoch
        self._serving_epoch = env.task_epoch
        self.counters["observations"] += 1
        self._serving = asyncio.create_task(self._serve(observation, decoded))

    async def _serve(self, observation: dataplane_pb2.Observation, inputs: ModelInputs) -> None:
        env = observation.envelope
        binding = self.client.runtime.binding
        # Past this age the robot would refuse the chunk anyway: stop waiting for it.
        limit_s = binding.profile.spec.freshness.max_observation_age_at_acceptance_us / 1e6
        try:
            output = await asyncio.wait_for(self.policy.infer(inputs), limit_s)
        except TimeoutError:
            self._reject(env, ReasonCode.WORKER_TIMED_OUT)
            return
        except PolicyError as exc:
            log.warning("the policy failed: %s", exc)
            self._reject(env, exc.reason, str(exc))
            return
        chunk = chunk_from(env, int(observation.anchor_index), output, binding, "local")
        if chunk is None:
            self._reject(env, ReasonCode.WORKER_OUTPUT_INVALID)
            return
        self.counters["chunks"] += 1
        self.client.on_gateway_message(
            dataplane_pb2.GatewayMessage(action_chunk=chunk), self._clock()
        )

    # --- helpers

    def _current(self, session_id: str, generation: int) -> bool:
        return bool(self._session_id) and (session_id, generation) == (
            self._session_id,
            self._generation,
        )

    def _reject(self, env: common_pb2.Envelope, code: ReasonCode, detail: str = "") -> None:
        self.counters[code.value] += 1
        rejected = dataplane_pb2.RequestRejected(envelope=env, reason=_reason(code, detail))
        self.client.on_gateway_message(
            dataplane_pb2.GatewayMessage(rejected=rejected), self._clock()
        )

    def _reply(self, handler: Callable[[Any, int], None], response: Any) -> None:
        # After this tick's messages are handed over, as a response from a gateway would be.
        asyncio.get_running_loop().call_soon(lambda: handler(response, self._clock()))

    def _cancel(self) -> None:
        if self._serving is not None:
            self._serving.cancel()
            self._serving = None


def _reason(code: ReasonCode, message: str = "") -> common_pb2.Reason:
    return common_pb2.Reason(code=code.value, message=message)
