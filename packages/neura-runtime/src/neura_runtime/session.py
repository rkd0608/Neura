"""Session handshake, lease heartbeats and RTT measurement around a ``RobotRuntime``.

Sans-IO like the runtime: adapters (gRPC, the simulator) move the messages this class emits and
deliver the responses. Round-trip times are measured on the robot's own clock only.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

import numpy as np

from neura.v1 import common_pb2, dataplane_pb2
from neura_contracts import ReasonCode
from neura_runtime.runtime import (
    SESSION_ENDING,
    AnnounceEpoch,
    RobotRuntime,
    SendObservation,
    reason_from,
)


@dataclass(frozen=True, slots=True)
class SessionConfig:
    robot_id: str
    revision_id: str
    # Used until real measurements exist. Deliberately pessimistic.
    initial_rtt_us: int = 20_000
    # RTT percentiles cover this much recent time, so one past spike does not dominate forever.
    rtt_window_us: int = 10_000_000
    # Heartbeat at least this often (and at least four times per lease ttl).
    heartbeat_interval_us: int = 250_000
    # Wait at least this long before retrying a rejected or failed open.
    reconnect_backoff_us: int = 500_000
    # Treat an open or control request as lost after this long without a response.
    response_timeout_us: int = 2_000_000


Channel = Literal["open", "heartbeat", "control", "stream"]


@dataclass(frozen=True, slots=True)
class Outbound:
    channel: Channel
    message: (
        dataplane_pb2.OpenSessionRequest
        | dataplane_pb2.HeartbeatRequest
        | dataplane_pb2.ControlRequest
        | dataplane_pb2.RobotMessage
    )


class RttEstimator:
    """Round-trip times measured on the robot clock over a sliding time window."""

    RECENT = 5

    def __init__(self, initial_us: int, window_us: int) -> None:
        self._initial = initial_us
        self._window_us = window_us
        self._samples: deque[tuple[int, int]] = deque()  # (measured at, rtt)

    def add(self, now_us: int, rtt_us: int) -> None:
        if rtt_us < 0:
            return
        self._samples.append((now_us, rtt_us))
        while self._samples and now_us - self._samples[0][0] > self._window_us:
            self._samples.popleft()

    def p99_us(self) -> int:
        """Conservative estimate for capacity planning; pessimistic until enough samples."""
        values = [rtt for _, rtt in self._samples]
        if len(values) < 8:
            return max(self._initial, max(values, default=0))
        return int(np.percentile(np.array(values, dtype=np.int64), 99))

    def recent_us(self) -> int:
        """Median of the latest few samples: follows the current link state quickly."""
        values = [rtt for _, rtt in list(self._samples)[-self.RECENT :]]
        if not values:
            return self._initial
        return int(np.median(np.array(values, dtype=np.int64)))


class SessionClient:
    def __init__(self, runtime: RobotRuntime, config: SessionConfig) -> None:
        self.runtime = runtime
        self.config = config
        self.rtt = RttEstimator(config.initial_rtt_us, config.rtt_window_us)
        self._opening_since: int | None = None
        self._next_open_at = 0
        self._lease_ttl_us = 0
        self._lease_deadline: int | None = None  # robot clock; conservative local estimate
        self._next_heartbeat_at = 0
        self._heartbeat_in_flight: int | None = None
        self.max_message_bytes = 0
        self.last_rejection: ReasonCode | None = None
        self.last_rejection_detail = ""

    @property
    def connected(self) -> bool:
        return self.runtime.session_id is not None

    def tick(self, now_us: int) -> list[Outbound]:
        """Advance timers and the runtime; return messages to send now."""
        out: list[Outbound] = []
        if not self.connected:
            if self._opening_since is not None and (
                now_us - self._opening_since > self.config.response_timeout_us
            ):
                self._opening_since = None
                self._next_open_at = now_us + self.config.reconnect_backoff_us
            if self._opening_since is None and now_us >= self._next_open_at:
                out.append(Outbound("open", self._open_request()))
                self._opening_since = now_us
        else:
            assert self._lease_deadline is not None
            if now_us >= self._lease_deadline:
                # Our local view of the lease ran out without a renewal: assume it is gone.
                self._lose_session(ReasonCode.LEASE_EXPIRED, now_us)
            elif now_us >= self._next_heartbeat_at and self._heartbeat_in_flight is None:
                out.append(Outbound("heartbeat", self._heartbeat(now_us)))

        self.runtime.tick(now_us)
        out.extend(self.drain())
        return out

    def _open_request(self) -> dataplane_pb2.OpenSessionRequest:
        binding = self.runtime.binding
        return dataplane_pb2.OpenSessionRequest(
            protocol_version=common_pb2.PROTOCOL_VERSION_1,
            robot_id=self.config.robot_id,
            revision_id=self.config.revision_id,
            artifact_digest=binding.artifact_digest,
            execution_profile_digest=binding.execution_profile_digest,
            robot_profile_digest=binding.robot_profile_digest,
        )

    def _heartbeat(self, now_us: int) -> dataplane_pb2.HeartbeatRequest:
        self._heartbeat_in_flight = now_us
        return dataplane_pb2.HeartbeatRequest(
            session_id=self.runtime.session_id or "",
            lease_generation=self.runtime.lease_generation or 0,
            robot_send_time_us=now_us,
            rtt_p99_us=self.rtt.p99_us(),
            rtt_recent_us=self.rtt.recent_us(),
            runtime_state=self.runtime.state.value,
            fault_reason=self.runtime.fault.reason.value if self.runtime.fault else "",
            dispatched_actions=self.runtime.dispatched,
            queued_actions=self.runtime.queued_actions,
            task_epoch=self.runtime.task_epoch,
        )

    def drain(self) -> list[Outbound]:
        """Messages the runtime emitted outside ``tick`` (for example after ``arm``)."""
        out: list[Outbound] = []
        for item in self.runtime.outbox:
            if isinstance(item, SendObservation):
                out.append(Outbound("stream", dataplane_pb2.RobotMessage(observation=item.message)))
            elif isinstance(item, AnnounceEpoch):
                out.append(Outbound("control", self._epoch_control(item)))
        self.runtime.outbox.clear()
        return out

    def _epoch_control(self, item: AnnounceEpoch) -> dataplane_pb2.ControlRequest:
        binding = self.runtime.binding
        return dataplane_pb2.ControlRequest(
            envelope=common_pb2.Envelope(
                protocol_version=common_pb2.PROTOCOL_VERSION_1,
                session_id=self.runtime.session_id or "",
                lease_generation=self.runtime.lease_generation or 0,
                task_epoch=item.current,
                artifact_digest=binding.artifact_digest,
                execution_profile_digest=binding.execution_profile_digest,
            ),
            epoch_advanced=dataplane_pb2.EpochAdvanced(
                previous_epoch=item.previous,
                cause=common_pb2.Reason(code=item.cause.value if item.cause else ""),
            ),
        )

    # --- responses ----------------------------------------------------------------------------

    def on_open_response(self, response: dataplane_pb2.OpenSessionResponse, now_us: int) -> None:
        sent_at = self._opening_since
        self._opening_since = None
        if response.WhichOneof("result") == "accepted":
            accepted = response.accepted
            self._lease_ttl_us = accepted.lease_ttl_us
            # The lease started no earlier than our send time; measure from there (conservative).
            start = sent_at if sent_at is not None else now_us
            self._lease_deadline = start + accepted.lease_ttl_us
            self._next_heartbeat_at = now_us
            self._heartbeat_in_flight = None
            self.max_message_bytes = accepted.max_message_bytes
            self.last_rejection = None
            self.last_rejection_detail = ""
            self.runtime.attach_session(accepted.session_id, accepted.lease_generation)
        else:
            self.last_rejection = reason_from(response.rejected.code)
            self.last_rejection_detail = response.rejected.message
            self._next_open_at = now_us + self.config.reconnect_backoff_us

    def on_open_failed(self, now_us: int) -> None:
        self._opening_since = None
        self._next_open_at = now_us + self.config.reconnect_backoff_us

    def on_heartbeat_response(self, response: dataplane_pb2.HeartbeatResponse, now_us: int) -> None:
        sent_at = self._heartbeat_in_flight
        self._heartbeat_in_flight = None
        if response.WhichOneof("result") == "ack":
            ack = response.ack
            if sent_at is None or ack.robot_send_time_us != sent_at:
                return  # a late answer to an older heartbeat
            self.rtt.add(now_us, now_us - sent_at)
            # The gateway renewed no earlier than our send time, so this deadline is conservative.
            self._lease_deadline = sent_at + ack.lease_remaining_us
            self._next_heartbeat_at = now_us + self._heartbeat_interval()
        else:
            # A refused renewal ends the session whatever the reason: the lease was not renewed.
            self._lose_session(reason_from(response.rejected.code), now_us)

    def _heartbeat_interval(self) -> int:
        return max(1, min(self._lease_ttl_us // 4, self.config.heartbeat_interval_us))

    def on_heartbeat_failed(self, now_us: int) -> None:
        self._heartbeat_in_flight = None
        self._next_heartbeat_at = now_us + max(self._lease_ttl_us // 10, 1)

    def on_control_response(self, response: dataplane_pb2.ControlResponse, now_us: int) -> None:
        if response.WhichOneof("result") == "rejected":
            reason = reason_from(response.rejected.code)
            if reason in SESSION_ENDING:
                self._lose_session(reason, now_us)

    def on_gateway_message(self, message: dataplane_pb2.GatewayMessage, now_us: int) -> None:
        kind = message.WhichOneof("body")
        if kind == "action_chunk":
            self.runtime.on_chunk(message.action_chunk, now_us)
        elif kind == "rejected":
            self.runtime.on_rejected(message.rejected, now_us)  # detaches if it ends the session
            if not self.connected:
                self._next_open_at = now_us + self.config.reconnect_backoff_us

    def on_stream_closed(self, now_us: int) -> None:
        """The data stream broke. The lease may still be valid; the adapter reopens the stream."""

    def _lose_session(self, reason: ReasonCode, now_us: int) -> None:
        self.runtime.detach_session(reason)
        self._lease_deadline = None
        self._heartbeat_in_flight = None
        self._next_open_at = now_us + self.config.reconnect_backoff_us

    def close(self) -> dataplane_pb2.ControlRequest | None:
        if not self.connected:
            return None
        binding = self.runtime.binding
        request = dataplane_pb2.ControlRequest(
            envelope=common_pb2.Envelope(
                protocol_version=common_pb2.PROTOCOL_VERSION_1,
                session_id=self.runtime.session_id or "",
                lease_generation=self.runtime.lease_generation or 0,
                task_epoch=self.runtime.task_epoch,
                artifact_digest=binding.artifact_digest,
                execution_profile_digest=binding.execution_profile_digest,
            ),
            close=dataplane_pb2.Close(cause=common_pb2.Reason(code="")),
        )
        self.runtime.detach_session(ReasonCode.SESSION_UNKNOWN)
        return request
