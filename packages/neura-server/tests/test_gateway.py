from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from neura.v1 import common_pb2, dataplane_pb2
from neura_adapters import MockPolicy, ModelInputs, PolicyError
from neura_contracts import ReasonCode
from neura_runtime.runtime import RobotRuntime, RuntimeState, SendObservation
from neura_runtime.session import Outbound
from neura_runtime.testing import FakeController, FakeSensors
from neura_server.gateway import Deployment
from neura_server.inputs import InvalidObservation, decode_observation
from neura_server.testing import GW0, MS, R0, Robot, make_core


def test_round_trip_through_gateway_and_policy(make_binding: Any) -> None:
    binding = make_binding()
    core = make_core(binding, ["r1"])
    robot = Robot(binding, "r1")
    assert robot.open(core, GW0).WhichOneof("result") == "accepted"
    robot.runtime.arm(robot.now)
    message = robot.observation()
    assert core.receive("r1", message, message.ByteSize(), GW0 + 1 * MS) == []
    batch, dropped = core.next_batch(next(iter(core.groups)), GW0 + 2 * MS)
    assert dropped == []
    assert batch is not None
    outputs = MockPolicy(binding).infer([r.inputs for r in batch.requests])
    deliveries = core.complete(next(iter(core.groups)), batch, outputs, GW0 + 20 * MS)
    assert len(deliveries) == 1
    chunk = deliveries[0].message.action_chunk
    assert chunk.envelope == message.observation.envelope
    robot.now += 10 * MS
    robot.deliver(deliveries)
    assert robot.runtime.state is RuntimeState.RUNNING


def test_open_rejections(make_binding: Any) -> None:
    binding = make_binding()
    core = make_core(binding, ["r1"])
    robot = Robot(binding, "r1")
    request = robot.client.tick(R0)[0].message
    assert isinstance(request, dataplane_pb2.OpenSessionRequest)

    def code(name: str, req: dataplane_pb2.OpenSessionRequest) -> str:
        return core.open_session(name, req, GW0).rejected.code

    assert code("intruder", request) == "AUTH_DEVICE_UNKNOWN"
    other = dataplane_pb2.OpenSessionRequest()
    other.CopyFrom(request)
    other.robot_id = ""
    assert code("unknown-robot", other) == "DEPLOYMENT_NOT_ACTIVE"
    other.CopyFrom(request)
    other.revision_id = "rev_2"
    assert code("r1", other) == "DEPLOYMENT_NOT_ACTIVE"
    other.CopyFrom(request)
    other.protocol_version = common_pb2.PROTOCOL_VERSION_UNSPECIFIED
    assert code("r1", other) == "PROTOCOL_VERSION_MISMATCH"
    assert core.open_session("r1", request, GW0).WhichOneof("result") == "accepted"
    assert code("r1", request) == "LEASE_BUSY"


def test_admission_rejects_and_releases_the_lease(make_binding: Any) -> None:
    binding = make_binding()
    names = [f"r{i}" for i in range(64)]
    core = make_core(binding, names, reserve=0.95)
    results = [Robot(binding, n).open(core, GW0) for n in names]
    rejected = [r for r in results if r.WhichOneof("result") == "rejected"]
    assert rejected
    assert rejected[0].rejected.code == "ADMISSION_CAPACITY_EXCEEDED"
    assert len(core.leases.active()) == len(names) - len(rejected)


def test_observation_rejections(make_binding: Any) -> None:
    binding = make_binding()
    core = make_core(binding, ["r1"], max_message_bytes=10_000_000)
    robot = Robot(binding, "r1")
    robot.open(core, GW0)
    robot.runtime.arm(robot.now)
    message = robot.observation()

    def reason(
        msg: dataplane_pb2.RobotMessage, *, robot_id: str = "r1", size: int | None = None
    ) -> str:
        out = core.receive(robot_id, msg, msg.ByteSize() if size is None else size, GW0)
        assert len(out) == 1
        return out[0].message.rejected.reason.code

    assert reason(message, size=20_000_000) == "PROTOCOL_MESSAGE_TOO_LARGE"
    assert reason(message, robot_id="r2") == "AUTH_DEVICE_UNKNOWN"
    tampered = dataplane_pb2.RobotMessage()
    tampered.CopyFrom(message)
    tampered.observation.envelope.lease_generation += 1
    assert reason(tampered) == "LEASE_GENERATION_STALE"
    tampered.CopyFrom(message)
    tampered.observation.envelope.execution_profile_digest = "sha256:" + "0" * 64
    assert reason(tampered) == "DEPLOYMENT_NOT_ACTIVE"
    tampered.CopyFrom(message)
    tampered.observation.images.pop()
    assert reason(tampered) == "PROTOCOL_MESSAGE_INVALID"
    # Accept one at epoch 1, then an older epoch is stale.
    assert core.receive("r1", message, message.ByteSize(), GW0) == []
    tampered.CopyFrom(message)
    tampered.observation.envelope.task_epoch = 0
    assert reason(tampered) == "SESSION_EPOCH_STALE"


def test_worker_failures_become_typed_rejections(make_binding: Any) -> None:
    binding = make_binding()
    core = make_core(binding, ["r1"])
    group = next(iter(core.groups))
    robot = Robot(binding, "r1")
    robot.open(core, GW0)
    robot.runtime.arm(robot.now)
    cases: list[tuple[list[np.ndarray[Any, Any]] | PolicyError, str]] = [
        (PolicyError("boom"), "WORKER_INFERENCE_FAILED"),
        (PolicyError("gone", reason=ReasonCode.WORKER_CRASHED), "WORKER_CRASHED"),
        (PolicyError("oom", out_of_memory=True), "WORKER_OUT_OF_MEMORY"),
        (
            [np.full((binding.chunk_length, binding.action_dim), np.nan, dtype=np.float32)],
            "WORKER_OUTPUT_INVALID",
        ),
    ]
    for error, expected in cases:
        message = robot.observation()
        core.receive("r1", message, message.ByteSize(), GW0)
        batch, _ = core.next_batch(group, GW0)
        assert batch is not None
        out = core.complete(group, batch, error, GW0 + 1)
        assert out[0].message.rejected.reason.code == expected
        robot.deliver(out)
        assert robot.runtime.state is RuntimeState.ARMED


def test_epoch_control_cancels_queued_work(make_binding: Any) -> None:
    binding = make_binding()
    core = make_core(binding, ["r1"])
    robot = Robot(binding, "r1")
    robot.open(core, GW0)
    robot.runtime.arm(robot.now)
    message = robot.observation()
    core.receive("r1", message, message.ByteSize(), GW0)
    robot.runtime.operator_takeover(robot.now)
    control = next(o for o in robot.client.drain() if o.channel == "control").message
    assert isinstance(control, dataplane_pb2.ControlRequest)
    assert core.control("r1", control, GW0).WhichOneof("result") == "ack"
    group = next(iter(core.groups))
    assert core.groups[group].scheduler.queued == 0


def test_close_releases_lease_and_admission(make_binding: Any) -> None:
    binding = make_binding()
    core = make_core(binding, ["r1"])
    robot = Robot(binding, "r1")
    robot.open(core, GW0)
    close = robot.client.close()
    assert close is not None
    assert core.control("r1", close, GW0).WhichOneof("result") == "ack"
    assert core.leases.active() == []
    assert core.sessions == {}


def test_lease_sweep_forgets_sessions(make_binding: Any) -> None:
    binding = make_binding()
    core = make_core(binding, ["r1"])
    Robot(binding, "r1").open(core, GW0)
    core.sweep(GW0 + 2_000_001)
    assert core.sessions == {}
    assert core.status()["groups"][next(iter(core.groups))]["admitted_sessions"] == 0


def test_decode_observation_checks_everything(make_binding: Any) -> None:
    binding = make_binding("smolvla")
    runtime = RobotRuntime(binding, FakeController(), FakeSensors(binding))
    runtime.attach_session("s", 1)
    runtime.set_instruction("pick")
    runtime.arm(R0)
    runtime.tick(R0 + 1)
    observation = next(i.message for i in runtime.outbox if isinstance(i, SendObservation))
    decoded = decode_observation(observation, binding)
    assert isinstance(decoded, ModelInputs)
    assert decoded.instruction == "pick"
    assert decoded.images["front"].shape == (480, 640, 3)

    def invalid(mutate: Any) -> None:
        copy = dataplane_pb2.Observation()
        copy.CopyFrom(observation)
        mutate(copy)
        assert isinstance(decode_observation(copy, binding), InvalidObservation)

    invalid(lambda o: setattr(o, "instruction", ""))
    invalid(lambda o: setattr(o, "instruction", "x" * 300))
    invalid(lambda o: setattr(o.images[0], "width", 320))
    invalid(lambda o: setattr(o.images[0], "data", b"\x00" * 10))
    invalid(lambda o: setattr(o.images[0], "encoding", common_pb2.IMAGE_ENCODING_JPEG))
    invalid(lambda o: setattr(o.images[0], "color_order", common_pb2.COLOR_ORDER_BGR))
    invalid(lambda o: o.images.append(o.images[0]))
    invalid(lambda o: o.state["observation.state"].shape.append(1))
    invalid(
        lambda o: setattr(
            o.state["observation.state"], "data", np.full(6, np.inf, dtype=np.float32).tobytes()
        )
    )
    invalid(lambda o: o.frozen_prefix.shape.append(1))


def test_a_tensor_numpy_cannot_build_is_rejected_not_raised(make_binding: Any) -> None:
    # A robot's frozen prefix is decoded without an expected shape; 70 dimensions is more than
    # numpy builds, and the gateway must answer with a typed rejection.
    binding = make_binding("smolvla")
    core = make_core(binding, ["r1"])
    robot = Robot(binding, "r1")
    robot.open(core, GW0)
    robot.runtime.set_instruction("pick")
    robot.runtime.arm(robot.now)
    message = robot.observation()
    message.observation.frozen_prefix.CopyFrom(
        common_pb2.Tensor(dtype=common_pb2.DTYPE_FLOAT32, shape=[1] * 70, data=b"\0" * 4)
    )
    (delivery,) = core.receive("r1", message, message.ByteSize(), GW0)
    assert delivery.message.rejected.reason.code == ReasonCode.PROTOCOL_MESSAGE_INVALID.value


@pytest.mark.parametrize("model", ["act", "smolvla"])
def test_mock_policy_outputs_are_in_range(make_binding: Any, model: str) -> None:
    binding = make_binding(model)
    inputs = ModelInputs(
        state={"observation.state": np.full(6, 50.0, dtype=np.float32)},
        images={},
        instruction="x",
        frozen_prefix=None,
    )
    (out,) = MockPolicy(binding).infer([inputs])
    assert out.shape == (binding.chunk_length, binding.action_dim)
    robot = binding.action_transform.to_robot(out.astype(np.float64))
    assert binding.action_transform.out_of_range(robot) == []
    assert isinstance(Outbound("stream", dataplane_pb2.RobotMessage()), Outbound)


def test_a_robot_may_run_either_revision_during_a_switch(make_binding: Any) -> None:
    binding = make_binding()
    core = make_core(binding, ["r1", "r2"])
    group = next(iter(core.groups.values()))
    both = [
        Deployment("rev_1", binding, frozenset({"r1", "r2"})),
        Deployment("rev_2", binding, frozenset({"r1", "r2"})),
    ]
    assert core.replace_deployments(both) == []
    assert core.allowed_revisions("r1") == ["rev_1", "rev_2"]

    old = Robot(binding, "r1", "rev_1")
    new = Robot(binding, "r2", "rev_2")
    assert old.open(core, GW0).WhichOneof("result") == "accepted"
    assert new.open(core, GW0).WhichOneof("result") == "accepted"
    assert group is next(iter(core.groups.values()))  # same model and profile: one batch group

    # The switch completes: only rev_2 stays. The rev_1 session is closed, rev_2 keeps running.
    closed = core.replace_deployments([Deployment("rev_2", binding, frozenset({"r1", "r2"}))])
    assert len(closed) == 1
    assert [s.deployment.revision_id for s in core.sessions.values()] == ["rev_2"]
    again = Robot(binding, "r1", "rev_1").open(core, GW0 + 1 * MS)
    assert again.WhichOneof("result") == "rejected"
    assert again.rejected.code == ReasonCode.DEPLOYMENT_NOT_ACTIVE.value
    assert Robot(binding, "r1", "rev_2").open(core, GW0 + 2 * MS).WhichOneof("result") == "accepted"


def test_replacing_deployments_without_a_batch_group_is_refused(make_binding: Any) -> None:
    binding = make_binding("smolvla")
    core = make_core(make_binding(), ["r1"])
    with pytest.raises(ValueError, match="no batch group"):
        core.replace_deployments([Deployment("rev_x", binding, frozenset({"r1"}))])
    assert core.allowed_revisions("r1") == ["rev_1"]  # unchanged
