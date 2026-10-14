from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from neura.v1 import common_pb2, dataplane_pb2
from neura_contracts import FaultResponse, ReasonCode
from neura_runtime import tensors
from neura_runtime.binding import ExecutionBinding
from neura_runtime.runtime import (
    AnnounceEpoch,
    RobotRuntime,
    RuntimeConfig,
    RuntimeState,
    SendObservation,
    TransitionError,
)
from neura_runtime.testing import FakeController, FakeSensors

PERIOD = 33_333
T0 = 5_000_000_000  # robot clock; deliberately unrelated to any other clock


class Harness:
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
        values: np.ndarray | None = None,
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


@pytest.fixture
def harness(make_binding: Any) -> Harness:
    return Harness(make_binding())


def test_lifecycle_to_running(harness: Harness) -> None:
    rt = harness.runtime
    assert rt.snapshot().state is RuntimeState.DISCONNECTED
    rt.attach_session("ses_a", 7)
    assert rt.snapshot().state is RuntimeState.READY
    rt.arm(harness.now)
    assert rt.snapshot().state is RuntimeState.ARMED
    assert rt.task_epoch == 1
    assert isinstance(rt.outbox[0], AnnounceEpoch)
    rt.outbox.clear()
    obs = harness.tick_for_observation()
    env = obs.envelope
    assert (env.session_id, env.lease_generation, env.task_epoch, env.sequence) == (
        "ses_a",
        7,
        1,
        1,
    )
    assert env.artifact_digest == harness.binding.artifact_digest
    assert obs.anchor_index == 0
    assert obs.budget_us == 0  # nothing queued: already starved
    assert set(obs.state) == {"observation.state"}
    assert {i.camera_role for i in obs.images} == {"front", "wrist"}
    assert rt.on_chunk(harness.chunk_for(obs), harness.now)
    assert rt.snapshot().state is RuntimeState.RUNNING
    harness.tick(3)
    assert len(harness.controller.commands) == 3


def test_arm_requires_session_and_authority(harness: Harness) -> None:
    with pytest.raises(TransitionError):
        harness.runtime.arm(harness.now)
    harness.runtime.attach_session("ses_a", 1)
    harness.controller.authority = False
    with pytest.raises(TransitionError):
        harness.runtime.arm(harness.now)


def test_refill_request_carries_budget(harness: Harness) -> None:
    rt = harness.runtime
    obs = harness.start()
    rt.on_chunk(harness.chunk_for(obs), harness.now)
    # 50 actions per chunk, refill at 20 queued: requests start after 30 dispatches.
    sent = [i for i in harness.tick(29) if isinstance(i, SendObservation)]
    assert sent == []
    obs2 = harness.tick_for_observation()
    assert obs2.queued_actions == 20
    assert obs2.anchor_index == 30
    assert obs2.budget_us == 20 * PERIOD - PERIOD  # default safety margin: one period
    assert obs2.envelope.sequence == 2


def test_one_outstanding_request(harness: Harness) -> None:
    harness.start()
    assert not [i for i in harness.tick(3) if isinstance(i, SendObservation)]


def test_request_timeout_frees_the_slot(harness: Harness) -> None:
    harness.start()
    timeout = harness.binding.profile.spec.freshness.request_timeout_us
    out = harness.tick(timeout // PERIOD + 2)
    assert harness.runtime.counters[ReasonCode.REQUEST_TIMED_OUT.value] >= 1
    assert any(isinstance(i, SendObservation) for i in out)


REJECTIONS = {
    "other session": ({"session_id": "ses_b"}, ReasonCode.LEASE_GENERATION_STALE),
    "old lease generation": ({"lease_generation": 6}, ReasonCode.LEASE_GENERATION_STALE),
    "other epoch": ({"task_epoch": 99}, ReasonCode.CHUNK_EPOCH_MISMATCH),
    "unexpected sequence": ({"sequence": 42}, ReasonCode.CHUNK_UNEXPECTED),
    "other revision": (
        {"artifact_digest": "sha256:" + "f" * 64},
        ReasonCode.CHUNK_REVISION_MISMATCH,
    ),
}


@pytest.mark.parametrize(("overrides", "reason"), REJECTIONS.values(), ids=REJECTIONS.keys())
def test_identity_rejections(
    harness: Harness, overrides: dict[str, Any], reason: ReasonCode
) -> None:
    obs = harness.start()
    assert not harness.runtime.on_chunk(harness.chunk_for(obs, **overrides), harness.now)
    assert harness.runtime.counters[reason.value] == 1
    assert harness.runtime.state is RuntimeState.ARMED


def test_duplicate_chunk_is_ignored(harness: Harness) -> None:
    obs = harness.start()
    chunk = harness.chunk_for(obs)
    assert harness.runtime.on_chunk(chunk, harness.now)
    queued = harness.runtime.queued_actions
    assert not harness.runtime.on_chunk(chunk, harness.now)
    assert harness.runtime.counters[ReasonCode.CHUNK_DUPLICATE.value] == 1
    assert harness.runtime.queued_actions == queued


def test_stale_observation_is_rejected(harness: Harness) -> None:
    obs = harness.start()
    budget = harness.binding.profile.spec.freshness.max_observation_age_at_acceptance_us
    late = harness.now + budget + 1
    assert not harness.runtime.on_chunk(harness.chunk_for(obs), late)
    assert harness.runtime.counters[ReasonCode.CHUNK_STALE_OBSERVATION.value] == 1


def test_payload_tampering_is_detected(harness: Harness) -> None:
    obs = harness.start()
    chunk = harness.chunk_for(obs)
    chunk.actions.data = bytes(len(chunk.actions.data) - 1) + b"\x01"
    assert not harness.runtime.on_chunk(chunk, harness.now)
    assert harness.runtime.counters[ReasonCode.CHUNK_INTEGRITY_FAILED.value] == 1


def test_wrong_shape_and_anchor_are_rejected(harness: Harness) -> None:
    obs = harness.start()
    bad = harness.chunk_for(obs, values=np.zeros((5, harness.binding.action_dim)))
    assert not harness.runtime.on_chunk(bad, harness.now)
    obs = harness.tick_for_observation()
    moved = harness.chunk_for(obs)
    moved.start_index += 1
    assert not harness.runtime.on_chunk(moved, harness.now)
    assert harness.runtime.counters[ReasonCode.CHUNK_INTEGRITY_FAILED.value] == 2


def test_non_finite_actions_are_rejected(harness: Harness) -> None:
    obs = harness.start()
    values = np.zeros((harness.binding.chunk_length, harness.binding.action_dim), dtype=np.float32)
    values[3, 1] = np.nan
    assert not harness.runtime.on_chunk(harness.chunk_for(obs, values=values), harness.now)
    assert harness.runtime.state is RuntimeState.ARMED


def test_out_of_range_action_latches_a_fault(harness: Harness) -> None:
    obs = harness.start()
    values = np.zeros((harness.binding.chunk_length, harness.binding.action_dim), dtype=np.float32)
    values[10, 0] = 500.0  # shoulder_pan range is +-110 deg
    assert not harness.runtime.on_chunk(harness.chunk_for(obs, values=values), harness.now)
    rt = harness.runtime
    assert rt.snapshot().state is RuntimeState.FAULTED
    assert rt.fault is not None
    assert rt.fault.reason is ReasonCode.ACTION_OUT_OF_RANGE
    assert rt.fault.response is FaultResponse.CONTROLLED_STOP
    assert harness.controller.commands == []


def test_out_of_range_beyond_executed_window_is_ignored(harness: Harness) -> None:
    obs = harness.start()
    values = np.zeros((harness.binding.chunk_length, harness.binding.action_dim), dtype=np.float32)
    values[90, 0] = 500.0  # beyond actions_per_chunk (50): never executed
    assert harness.runtime.on_chunk(harness.chunk_for(obs, values=values), harness.now)


def test_starvation_holds_then_faults_after_grace(harness: Harness) -> None:
    rt = harness.runtime
    obs = harness.start()
    rt.on_chunk(harness.chunk_for(obs), harness.now)
    epoch = rt.task_epoch
    harness.tick(51)
    assert rt.snapshot().state is RuntimeState.WAITING
    assert harness.controller.responses == [FaultResponse.HOLD_POSITION]
    assert rt.task_epoch == epoch + 1  # require_new_epoch_on_resume
    grace = harness.binding.profile.spec.starvation.grace_us
    harness.tick(grace // PERIOD + 2)
    assert rt.snapshot().state is RuntimeState.FAULTED
    assert rt.fault is not None
    assert rt.fault.reason is ReasonCode.STARVATION_GRACE_EXHAUSTED


def test_waiting_recovers_with_fresh_chunk_only(harness: Harness) -> None:
    rt = harness.runtime
    obs = harness.start()
    rt.on_chunk(harness.chunk_for(obs), harness.now)
    sent = [i.message for i in harness.tick(51) if isinstance(i, SendObservation)]
    late_obs, fresh = sent[0], sent[-1]
    assert late_obs.envelope.task_epoch == 1
    # A reply to the pre-starvation request belongs to the old epoch and is fenced.
    assert not rt.on_chunk(harness.chunk_for(late_obs), harness.now)
    assert rt.snapshot().state is RuntimeState.WAITING
    # Entering Waiting started a new epoch and immediately requested a fresh observation.
    assert fresh.envelope.task_epoch == 2
    assert rt.on_chunk(harness.chunk_for(fresh), harness.now)
    assert rt.snapshot().state is RuntimeState.RUNNING


def test_fault_is_latched_until_explicit_reset(harness: Harness) -> None:
    rt = harness.runtime
    harness.start()
    harness.controller.authority = False
    harness.tick()
    assert rt.snapshot().state is RuntimeState.FAULTED
    assert rt.fault is not None
    assert rt.fault.reason is ReasonCode.ACTUATOR_LOCK_LOST
    harness.controller.authority = True
    harness.tick(5)
    assert rt.snapshot().state is RuntimeState.FAULTED
    with pytest.raises(TransitionError):
        rt.reset(harness.now, revalidated=False)
    epoch = rt.task_epoch
    rt.reset(harness.now, revalidated=True)
    assert rt.snapshot().state is RuntimeState.READY
    assert rt.task_epoch == epoch + 1
    assert rt.fault is None


def test_controller_failure_uses_independent_stop_path(make_binding: Any) -> None:
    def add_stop_path(a: dict[str, Any], r: dict[str, Any], p: dict[str, Any]) -> None:
        r["spec"]["fault_responses"] = ["hold_position", "controlled_stop", "customer_stop_path"]

    h = Harness(make_binding(edit=add_stop_path))
    obs = h.start()
    h.runtime.on_chunk(h.chunk_for(obs), h.now)
    h.controller.fail = True
    h.tick()
    fault = h.runtime.fault
    assert fault is not None
    assert fault.reason is ReasonCode.CONTROLLER_COMMAND_FAILED
    assert fault.response is FaultResponse.CUSTOMER_STOP_PATH
    assert fault.response_confirmed


def test_unconfirmed_starvation_response_faults(harness: Harness) -> None:
    rt = harness.runtime
    obs = harness.start()
    rt.on_chunk(harness.chunk_for(obs), harness.now)
    harness.controller.confirm = False
    harness.tick(51)
    assert rt.snapshot().state is RuntimeState.FAULTED
    assert rt.fault is not None
    assert not rt.fault.response_confirmed


def test_operator_takeover_invalidates_policy_actions(harness: Harness) -> None:
    rt = harness.runtime
    obs = harness.start()
    rt.on_chunk(harness.chunk_for(obs), harness.now)
    harness.tick(29)
    in_flight = harness.tick_for_observation()
    rt.operator_takeover(harness.now)
    assert rt.snapshot().state is RuntimeState.READY
    assert rt.queued_actions == 0
    assert not rt.on_chunk(harness.chunk_for(in_flight), harness.now)
    commands = len(harness.controller.commands)
    harness.tick(5)
    assert len(harness.controller.commands) == commands


def test_complete_task_requires_confirmed_stop(harness: Harness) -> None:
    rt = harness.runtime
    obs = harness.start()
    rt.on_chunk(harness.chunk_for(obs), harness.now)
    rt.complete_task(harness.now)
    assert rt.snapshot().state is RuntimeState.READY
    assert harness.controller.responses[-1] is FaultResponse.CONTROLLED_STOP


def test_detached_session_drains_then_starves(harness: Harness) -> None:
    rt = harness.runtime
    obs = harness.start()
    rt.on_chunk(harness.chunk_for(obs), harness.now)
    rt.detach_session(ReasonCode.LEASE_EXPIRED)
    out = harness.tick(50)
    assert not [i for i in out if isinstance(i, SendObservation)]
    assert len(harness.controller.commands) == 50
    harness.tick()
    assert rt.snapshot().state is RuntimeState.WAITING


@pytest.mark.parametrize("problem", ["unavailable", "missing camera", "skew", "old"])
def test_incomplete_sensors_send_nothing(harness: Harness, problem: str) -> None:
    rt = harness.runtime
    rt.attach_session("ses_a", 1)
    rt.arm(harness.now)
    rt.outbox.clear()
    if problem == "unavailable":
        harness.sensors.unavailable = True
    elif problem == "missing camera":
        harness.sensors.missing.add("wrist")
    elif problem == "skew":
        harness.sensors.camera_lag_us = RuntimeConfig().max_sensor_skew_us + 1
    else:
        harness.sensors.age_us = (
            harness.binding.profile.spec.freshness.max_observation_age_at_dispatch_us + 1
        )
    out = harness.tick()
    assert not [i for i in out if isinstance(i, SendObservation)]
    assert rt.counters[ReasonCode.OBSERVATION_INCOMPLETE.value] == 1


def test_instruction_rules(make_binding: Any) -> None:
    h = Harness(make_binding("smolvla"))
    rt = h.runtime
    rt.attach_session("ses_a", 1)
    with pytest.raises(TransitionError):
        rt.arm(h.now)  # instruction required
    with pytest.raises(ValueError, match="maximum length"):
        rt.set_instruction("x" * 300)
    rt.set_instruction("pack the box")
    rt.arm(h.now)
    rt.outbox.clear()
    obs = h.tick_for_observation()
    assert obs.instruction == "pack the box"
    rt.on_chunk(h.chunk_for(obs), h.now)
    with pytest.raises(TransitionError):
        rt.set_instruction("unpack the box")


def test_reorder_and_unit_transforms_apply_to_dispatched_commands(make_binding: Any) -> None:
    def edit(a: dict[str, Any], r: dict[str, Any], p: dict[str, Any]) -> None:
        features = a["spec"]["outputs"]["features"]
        features[0], features[1] = features[1], features[0]
        features[0]["unit"] = "rad"  # shoulder_lift now first, in radians
        p["spec"]["transforms"] = [
            {
                "kind": "reorder_actions",
                "order": [c["name"] for c in r["spec"]["command_interface"]],
            },
            {
                "kind": "convert_unit",
                "target": "action",
                "feature": "shoulder_lift.pos",
                "from_unit": "rad",
                "to_unit": "deg",
            },
        ]

    h = Harness(make_binding(edit=edit))
    obs = h.start()
    values = np.zeros((h.binding.chunk_length, h.binding.action_dim), dtype=np.float32)
    values[:, 0] = 1.0  # model column 0: shoulder_lift, 1 rad
    values[:, 1] = 5.0  # model column 1: shoulder_pan, 5 deg
    assert h.runtime.on_chunk(h.chunk_for(obs, values=values), h.now)
    h.tick()
    command = h.controller.commands[0]
    assert command[0] == pytest.approx(5.0)  # robot order: shoulder_pan first
    assert command[1] == pytest.approx(np.degrees(1.0), rel=1e-6)
