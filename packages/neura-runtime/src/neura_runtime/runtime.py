"""The robot runtime: the only execution authority in Neura (design §5-§8).

Sans-IO: the runtime never reads a clock, sleeps or performs network I/O. The integration calls
``tick(now_us)`` once per control period with the robot's monotonic time, delivers incoming
messages with ``on_chunk`` / ``on_rejected``, and drains ``outbox`` to send what the runtime emits.
Every timestamp it handles comes from the robot's own clock.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from neura.v1 import common_pb2, dataplane_pb2
from neura_contracts import (
    REASON_CODES,
    BlendMerge,
    FaultResponse,
    MergeMode,
    ReasonCode,
    RtcMerge,
)
from neura_runtime import tensors
from neura_runtime.action_queue import ActionQueue, MergeKind, MergeSettings
from neura_runtime.binding import ExecutionBinding
from neura_runtime.interfaces import Controller, SensorSnapshot, SensorSource


class RuntimeState(StrEnum):
    DISCONNECTED = "Disconnected"
    READY = "Ready"
    ARMED = "Armed"
    RUNNING = "Running"
    WAITING = "Waiting"
    FAULTED = "Faulted"


ACTIVE_STATES = frozenset({RuntimeState.ARMED, RuntimeState.RUNNING, RuntimeState.WAITING})


class TransitionError(RuntimeError):
    """An operation was requested in a state that does not allow it."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    # Subtracted from the starvation budget sent with each request (design §8).
    safety_margin_us: int | None = None  # default: one control period
    # Largest allowed difference between the oldest and newest sensor sample in one observation.
    max_sensor_skew_us: int = 50_000
    # Remember this many recently completed sequences to classify duplicates.
    duplicate_window: int = 64


@dataclass(frozen=True, slots=True)
class SendObservation:
    message: dataplane_pb2.Observation


@dataclass(frozen=True, slots=True)
class AnnounceEpoch:
    previous: int
    current: int
    cause: ReasonCode | None


Outgoing = SendObservation | AnnounceEpoch


@dataclass(frozen=True, slots=True)
class FaultRecord:
    reason: ReasonCode
    at_us: int
    response: FaultResponse | None
    response_confirmed: bool
    detail: str | None


@dataclass(slots=True)
class _Pending:
    sequence: int
    anchor: int
    capture_time_us: int
    sent_at_us: int


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    state: RuntimeState
    task_epoch: int
    cursor: int
    queued_actions: int
    pending_sequence: int | None
    session_id: str | None
    lease_generation: int | None
    fault: FaultRecord | None
    last_reason: ReasonCode | None
    counters: dict[str, int] = field(default_factory=dict)


class RobotRuntime:
    def __init__(
        self,
        binding: ExecutionBinding,
        controller: Controller,
        sensors: SensorSource,
        config: RuntimeConfig | None = None,
    ) -> None:
        self.binding = binding
        self._controller = controller
        self._sensors = sensors
        self._config = config or RuntimeConfig()
        spec = binding.profile.spec
        self._period = binding.control_period_us
        self._margin = (
            self._config.safety_margin_us
            if self._config.safety_margin_us is not None
            else self._period
        )
        self._refill = spec.chunk.refill_threshold_actions
        self._freshness = spec.freshness
        self._starvation = spec.starvation
        self._merge = self._merge_settings()
        self._robot_responses = list(binding.robot.spec.fault_responses)

        self._queue = ActionQueue(len(binding.robot.spec.command_interface))
        self.state = RuntimeState.DISCONNECTED
        self.task_epoch = 0
        self._session_id: str | None = None
        self._lease_generation: int | None = None
        self._sequence = 0
        self._pending: _Pending | None = None
        self._completed: deque[tuple[int, int]] = deque(maxlen=self._config.duplicate_window)
        self._waiting_since: int | None = None
        self._instruction = ""
        self.fault: FaultRecord | None = None
        self.last_reason: ReasonCode | None = None
        self.counters: Counter[str] = Counter()
        self.outbox: list[Outgoing] = []
        self.dispatched = 0
        # Called with (task_epoch, action_index) after each command the controller accepted.
        self.dispatch_listener: Callable[[int, int], None] | None = None

    # --- configuration ------------------------------------------------------------------------

    def _merge_settings(self) -> MergeSettings:
        spec = self.binding.profile.spec
        merge = spec.merge
        return MergeSettings(
            kind=MergeKind(merge.mode),
            actions_per_chunk=spec.chunk.actions_per_chunk,
            min_useful_actions=spec.chunk.min_useful_actions,
            blend_weights=tuple(merge.new_chunk_weights) if isinstance(merge, BlendMerge) else (),
            max_conditioned_delay=(
                merge.max_conditioned_delay_actions if isinstance(merge, RtcMerge) else 0
            ),
        )

    # --- session ------------------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def lease_generation(self) -> int | None:
        return self._lease_generation

    def attach_session(self, session_id: str, lease_generation: int) -> None:
        self._session_id = session_id
        self._lease_generation = lease_generation
        self._pending = None
        if self.state is RuntimeState.DISCONNECTED:
            self.state = RuntimeState.READY

    def detach_session(self, reason: ReasonCode) -> None:
        """The session or lease is gone. Queued, already-validated actions keep executing; with
        no new chunks the runtime starves and follows its starvation policy (design §15)."""
        self._session_id = None
        self._lease_generation = None
        self._pending = None
        self._note(reason)
        if self.state is RuntimeState.READY:
            self.state = RuntimeState.DISCONNECTED

    # --- operator and task lifecycle -----------------------------------------------------------

    def set_instruction(self, text: str) -> None:
        """Change the language instruction. Only allowed at a task boundary (Ready or Armed)."""
        if self.state not in (RuntimeState.DISCONNECTED, RuntimeState.READY, RuntimeState.ARMED):
            raise TransitionError("the instruction can only change at a task boundary")
        if len(text.encode("utf-8")) > max(self.binding.language_max_bytes, 0):
            raise ValueError("instruction exceeds the execution profile's maximum length")
        if text != self._instruction:
            self._instruction = text
            if self.state is RuntimeState.ARMED:
                self._advance_epoch(None)

    def arm(self, now_us: int) -> None:
        """Local activation permission: start a new task epoch and begin requesting actions."""
        if self.state is not RuntimeState.READY:
            raise TransitionError(f"cannot arm from {self.state}")
        if self._session_id is None:
            raise TransitionError("no session")
        if not self._controller.has_authority():
            raise TransitionError("the runtime does not hold actuator authority")
        if self.binding.language_input is not None and not self._instruction:
            raise TransitionError("this model requires an instruction before arming")
        self.state = RuntimeState.ARMED
        self._advance_epoch(None)

    def complete_task(self, now_us: int) -> None:
        """Stop at a task boundary. The controller must confirm the stop before Ready."""
        if self.state not in ACTIVE_STATES:
            raise TransitionError(f"no active task in {self.state}")
        self._queue.clear()
        self._pending = None
        response = self._preferred_stop()
        if response is not None and not self._controller.apply_response(response):
            self._fault(ReasonCode.CONTROLLER_COMMAND_FAILED, now_us, "stop not confirmed")
            return
        self.state = RuntimeState.READY if self._session_id else RuntimeState.DISCONNECTED
        self._advance_epoch(None)

    def operator_takeover(self, now_us: int) -> None:
        """Revoke policy authority and invalidate every queued and in-flight policy action."""
        if self.state is RuntimeState.FAULTED:
            return
        was_active = self.state in ACTIVE_STATES
        self._queue.clear()
        self._pending = None
        self.state = RuntimeState.READY if self._session_id else RuntimeState.DISCONNECTED
        self._note(ReasonCode.OPERATOR_TAKEOVER)
        if was_active:
            self._advance_epoch(ReasonCode.OPERATOR_TAKEOVER)

    def reset(self, now_us: int, *, revalidated: bool) -> None:
        """Explicit local reset of a latched fault. Requires revalidation and starts a new epoch."""
        if self.state is not RuntimeState.FAULTED:
            raise TransitionError(f"nothing to reset in {self.state}")
        if not revalidated:
            raise TransitionError("reset requires revalidation of the robot and contracts")
        self.fault = None
        self.state = RuntimeState.READY if self._session_id else RuntimeState.DISCONNECTED
        self._advance_epoch(None)

    # --- control loop -------------------------------------------------------------------------

    def tick(self, now_us: int) -> None:
        """Run one control period: dispatch, starvation handling, request scheduling."""
        if self.state not in ACTIVE_STATES:
            return
        if not self._controller.has_authority():
            self._fault(ReasonCode.ACTUATOR_LOCK_LOST, now_us, None)
            return
        if (
            self._pending is not None
            and now_us - self._pending.sent_at_us > self._freshness.request_timeout_us
        ):
            self._pending = None
            self._note(ReasonCode.REQUEST_TIMED_OUT)

        if self.state is RuntimeState.RUNNING:
            item = self._queue.pop()
            if item is None:
                self._enter_waiting(now_us)
            else:
                index, action = item
                result = self._controller.command(action)
                if not result.ok:
                    self._fault(ReasonCode.CONTROLLER_COMMAND_FAILED, now_us, result.detail)
                    return
                self.dispatched += 1
                if self.dispatch_listener is not None:
                    self.dispatch_listener(self.task_epoch, index)

        if self.state is RuntimeState.WAITING:
            assert self._waiting_since is not None
            if now_us - self._waiting_since > self._starvation.grace_us:
                self._fault(ReasonCode.STARVATION_GRACE_EXHAUSTED, now_us, None)
                return

        if self.state in ACTIVE_STATES and self._should_request():
            self._request(now_us)

    def _should_request(self) -> bool:
        if self._session_id is None or self._pending is not None:
            return False
        if self.state is RuntimeState.RUNNING:
            return len(self._queue) <= self._refill
        return True  # Armed or Waiting: the queue is empty

    def _enter_waiting(self, now_us: int) -> None:
        self.state = RuntimeState.WAITING
        self._waiting_since = now_us
        self._note(ReasonCode.BUFFER_STARVED)
        if not self._controller.apply_response(self._starvation.response):
            self._fault(ReasonCode.CONTROLLER_COMMAND_FAILED, now_us, "starvation response failed")
            return
        if self._starvation.require_new_epoch_on_resume:
            self._advance_epoch(ReasonCode.BUFFER_STARVED)

    def _request(self, now_us: int) -> None:
        snapshot = self._sensors.capture(now_us)
        observation = self._build_observation(snapshot, now_us)
        if observation is None:
            self._note(ReasonCode.OBSERVATION_INCOMPLETE)
            return
        self.outbox.append(SendObservation(observation))

    def _build_observation(
        self, snapshot: SensorSnapshot | None, now_us: int
    ) -> dataplane_pb2.Observation | None:
        binding = self.binding
        if snapshot is None:
            return None
        times = [snapshot.state_time_us] + [f.capture_time_us for f in snapshot.images.values()]
        oldest, newest = min(times), max(times)
        if newest - oldest > self._config.max_sensor_skew_us:
            return None
        if now_us - oldest > self._freshness.max_observation_age_at_dispatch_us:
            return None
        state: dict[str, common_pb2.Tensor] = {}
        for state_layout in binding.state_layouts:
            try:
                values = np.array(
                    [snapshot.state[name] for name in state_layout.feature_names],
                    dtype=np.float64,
                )
            except KeyError:
                return None
            if not np.isfinite(values).all():
                return None
            state[state_layout.input_name] = tensors.encode(
                (values * state_layout.factors).astype(np.float32)
            )
        images = []
        for layout in binding.image_layouts:
            frame = snapshot.images.get(layout.camera_role)
            if frame is None or (frame.width, frame.height) != (layout.width, layout.height):
                return None
            if len(frame.data) != frame.width * frame.height * 3:
                return None
            camera = next(c for c in binding.robot.spec.cameras if c.role == layout.camera_role)
            images.append(
                common_pb2.Image(
                    camera_role=layout.camera_role,
                    width=frame.width,
                    height=frame.height,
                    color_order=(
                        common_pb2.COLOR_ORDER_RGB
                        if camera.color_order.value == "rgb"
                        else common_pb2.COLOR_ORDER_BGR
                    ),
                    encoding=common_pb2.IMAGE_ENCODING_RAW,
                    data=frame.data,
                    capture_time_us=frame.capture_time_us,
                )
            )

        anchor = (
            self._queue.end_index if self._merge.kind is MergeKind.APPEND else self._queue.cursor
        )
        queued = len(self._queue)
        budget = max(0, queued * self._period - self._margin)
        self._sequence += 1
        message = dataplane_pb2.Observation(
            envelope=self._envelope(self._sequence),
            anchor_index=anchor,
            queued_actions=queued,
            budget_us=budget,
            capture_time_us=oldest,
            state=state,
            images=images,
            instruction=self._instruction,
        )
        if self._merge.kind is MergeKind.RTC:
            prefix = self._queue.peek(self._merge.max_conditioned_delay)
            if len(prefix):
                model_prefix = binding.action_transform.to_model(prefix)
                message.frozen_prefix.CopyFrom(tensors.encode(model_prefix.astype(np.float32)))
        self._pending = _Pending(self._sequence, anchor, oldest, now_us)
        return message

    def _envelope(self, sequence: int) -> common_pb2.Envelope:
        return common_pb2.Envelope(
            protocol_version=common_pb2.PROTOCOL_VERSION_1,
            session_id=self._session_id or "",
            lease_generation=self._lease_generation or 0,
            task_epoch=self.task_epoch,
            sequence=sequence,
            artifact_digest=self.binding.artifact_digest,
            execution_profile_digest=self.binding.execution_profile_digest,
        )

    # --- incoming messages --------------------------------------------------------------------

    def on_chunk(self, chunk: dataplane_pb2.ActionChunk, now_us: int) -> bool:
        """Validate and merge an action chunk (design §5.2). Returns True if accepted."""
        env = chunk.envelope
        if env.session_id != self._session_id or env.lease_generation != self._lease_generation:
            return self._drop(ReasonCode.LEASE_GENERATION_STALE)
        if env.task_epoch != self.task_epoch:
            return self._drop(ReasonCode.CHUNK_EPOCH_MISMATCH)
        pending = self._pending
        if pending is None or env.sequence != pending.sequence:
            if (env.task_epoch, env.sequence) in self._completed:
                return self._drop(ReasonCode.CHUNK_DUPLICATE)
            return self._drop(ReasonCode.CHUNK_UNEXPECTED)
        if self.state not in ACTIVE_STATES:
            return self._drop(ReasonCode.CHUNK_UNEXPECTED)

        # From here the chunk answers the outstanding request: any rejection frees it.
        self._pending = None
        self._completed.append((env.task_epoch, env.sequence))
        if (
            env.artifact_digest != self.binding.artifact_digest
            or env.execution_profile_digest != self.binding.execution_profile_digest
        ):
            return self._drop(ReasonCode.CHUNK_REVISION_MISMATCH)
        if now_us - pending.capture_time_us > self._freshness.max_observation_age_at_acceptance_us:
            return self._drop(ReasonCode.CHUNK_STALE_OBSERVATION)
        if (
            self.state is RuntimeState.WAITING
            and self._waiting_since is not None
            and pending.capture_time_us < self._waiting_since
        ):
            return self._drop(ReasonCode.CHUNK_STALE_OBSERVATION)
        if chunk.start_index != pending.anchor:
            return self._drop(ReasonCode.CHUNK_INTEGRITY_FAILED)
        if tensors.payload_digest(chunk.actions.data) != chunk.actions_digest.sha256:
            return self._drop(ReasonCode.CHUNK_INTEGRITY_FAILED)
        try:
            model_actions = tensors.decode(
                chunk.actions, shape=(self.binding.chunk_length, self.binding.action_dim)
            ).astype(np.float64)
        except tensors.TensorError:
            return self._drop(ReasonCode.CHUNK_INTEGRITY_FAILED)

        robot_actions = self.binding.action_transform.to_robot(model_actions)
        executable = robot_actions[: self._merge.actions_per_chunk]
        bad_rows = self.binding.action_transform.out_of_range(executable)
        if bad_rows:
            self._fault(
                ReasonCode.ACTION_OUT_OF_RANGE,
                now_us,
                f"rows {bad_rows[:5]} outside the robot's declared command ranges",
            )
            return False

        result = self._queue.merge(chunk.start_index, robot_actions, self._merge)
        if not result.accepted:
            assert result.reason is not None
            return self._drop(result.reason)
        self.counters["chunks_accepted"] += 1
        self.counters["actions_trimmed"] += result.trimmed
        if self.state in (RuntimeState.ARMED, RuntimeState.WAITING):
            self.state = RuntimeState.RUNNING
            self._waiting_since = None
        return True

    def on_rejected(self, message: dataplane_pb2.RequestRejected, now_us: int) -> None:
        env = message.envelope
        reason = _reason(message.reason.code)
        if env.session_id != self._session_id:
            # A late answer addressed to an earlier session (each lease has its own session id):
            # it says nothing about this one.
            self._note(ReasonCode.LEASE_GENERATION_STALE)
            return
        if (
            self._pending is not None
            and env.task_epoch == self.task_epoch
            and env.sequence == self._pending.sequence
        ):
            self._pending = None
        self._note(reason)
        if reason in (ReasonCode.LEASE_GENERATION_STALE, ReasonCode.LEASE_EXPIRED):
            self.detach_session(reason)

    # --- internals ----------------------------------------------------------------------------

    def _drop(self, reason: ReasonCode) -> bool:
        self._note(reason)
        return False

    def _note(self, reason: ReasonCode) -> None:
        self.last_reason = reason
        self.counters[reason.value] += 1

    def _advance_epoch(self, cause: ReasonCode | None) -> None:
        previous = self.task_epoch
        self.task_epoch += 1
        self._queue.reset_indexing()
        self._pending = None
        self._sequence = 0
        if self._session_id is not None:
            self.outbox.append(AnnounceEpoch(previous, self.task_epoch, cause))

    def _preferred_stop(self) -> FaultResponse | None:
        for response in (
            FaultResponse.CONTROLLED_STOP,
            FaultResponse.HOLD_POSITION,
            FaultResponse.BRAKE,
        ):
            if response in self._robot_responses:
                return response
        return None

    def _fault_response(self, reason: ReasonCode) -> FaultResponse | None:
        if reason is ReasonCode.CONTROLLER_COMMAND_FAILED:
            # The controller is not applying commands: use the customer's independent path.
            order: tuple[FaultResponse, ...] = (
                FaultResponse.CUSTOMER_STOP_PATH,
                FaultResponse.BRAKE,
                FaultResponse.CONTROLLED_STOP,
            )
        else:
            order = (
                FaultResponse.CONTROLLED_STOP,
                FaultResponse.CUSTOMER_STOP_PATH,
                FaultResponse.BRAKE,
                FaultResponse.HOLD_POSITION,
            )
        return next((r for r in order if r in self._robot_responses), None)

    def _fault(self, reason: ReasonCode, now_us: int, detail: str | None) -> None:
        """Latch a fault. The first fault is kept; nothing clears it except ``reset``."""
        self._note(reason)
        if self.state is RuntimeState.FAULTED:
            return
        self._queue.clear()
        self._pending = None
        self.state = RuntimeState.FAULTED
        response = self._fault_response(reason)
        confirmed = False
        if response is not None and reason is not ReasonCode.ACTUATOR_LOCK_LOST:
            confirmed = self._controller.apply_response(response)
        self.fault = FaultRecord(reason, now_us, response, confirmed, detail)

    def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            state=self.state,
            task_epoch=self.task_epoch,
            cursor=self._queue.cursor,
            queued_actions=len(self._queue),
            pending_sequence=self._pending.sequence if self._pending else None,
            session_id=self._session_id,
            lease_generation=self._lease_generation,
            fault=self.fault,
            last_reason=self.last_reason,
            counters=dict(self.counters),
        )

    @property
    def queued_actions(self) -> int:
        return len(self._queue)

    @property
    def merge_mode(self) -> MergeMode:
        return self.binding.merge_mode


def _reason(code: str) -> ReasonCode:
    try:
        reason = ReasonCode(code)
    except ValueError:
        return ReasonCode.PROTOCOL_MESSAGE_INVALID
    return reason if reason in REASON_CODES else ReasonCode.PROTOCOL_MESSAGE_INVALID
