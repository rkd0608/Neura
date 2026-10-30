from __future__ import annotations

from typing import Any

import pytest

from neura.v1 import common_pb2, dataplane_pb2
from neura_contracts import ReasonCode
from neura_runtime.runtime import RobotRuntime, RuntimeState
from neura_runtime.session import RttEstimator, SessionClient, SessionConfig
from neura_runtime.testing import FakeController, FakeSensors

T0 = 77_000_000


def _client(make_binding: Any) -> SessionClient:
    binding = make_binding()
    runtime = RobotRuntime(binding, FakeController(), FakeSensors(binding))
    return SessionClient(runtime, SessionConfig(robot_id="robot-1", revision_id="rev_1"))


def _accept(session_id: str = "ses_1", generation: int = 3) -> dataplane_pb2.OpenSessionResponse:
    return dataplane_pb2.OpenSessionResponse(
        accepted=dataplane_pb2.SessionAccepted(
            session_id=session_id,
            lease_generation=generation,
            lease_ttl_us=2_000_000,
            max_message_bytes=1 << 24,
        )
    )


def test_open_carries_identity_and_digests(make_binding: Any) -> None:
    client = _client(make_binding)
    out = client.tick(T0)
    assert [o.channel for o in out] == ["open"]
    request = out[0].message
    assert isinstance(request, dataplane_pb2.OpenSessionRequest)
    binding = client.runtime.binding
    assert request.robot_id == "robot-1"
    assert request.revision_id == "rev_1"
    assert request.artifact_digest == binding.artifact_digest
    assert request.robot_profile_digest == binding.robot_profile_digest
    # Only one open in flight.
    assert client.tick(T0 + 10) == []


def test_accept_attaches_and_heartbeats(make_binding: Any) -> None:
    client = _client(make_binding)
    client.tick(T0)
    client.on_open_response(_accept(), T0 + 1_000)
    assert client.runtime.state is RuntimeState.READY
    out = client.tick(T0 + 2_000)
    beats = [o for o in out if o.channel == "heartbeat"]
    assert len(beats) == 1
    beat = beats[0].message
    assert isinstance(beat, dataplane_pb2.HeartbeatRequest)
    assert (beat.session_id, beat.lease_generation) == ("ses_1", 3)
    ack = dataplane_pb2.HeartbeatResponse(
        ack=dataplane_pb2.HeartbeatAck(
            robot_send_time_us=beat.robot_send_time_us, lease_remaining_us=2_000_000
        )
    )
    client.on_heartbeat_response(ack, T0 + 2_000 + 1_500)
    assert client.rtt.p99_us() >= 1_500
    assert client.rtt.recent_us() == 1_500


def test_rejected_open_backs_off(make_binding: Any) -> None:
    client = _client(make_binding)
    client.tick(T0)
    client.on_open_response(
        dataplane_pb2.OpenSessionResponse(rejected=common_pb2.Reason(code="LEASE_BUSY")), T0 + 10
    )
    assert client.last_rejection is ReasonCode.LEASE_BUSY
    assert client.tick(T0 + 20) == []
    assert [o.channel for o in client.tick(T0 + 10 + 500_001)] == ["open"]


def test_lost_open_response_times_out(make_binding: Any) -> None:
    client = _client(make_binding)
    client.tick(T0)
    timeout = client.config.response_timeout_us
    client.tick(T0 + timeout + 1)  # gives up on the lost open, backs off
    out = client.tick(T0 + timeout + 1 + client.config.reconnect_backoff_us)
    assert [o.channel for o in out] == ["open"]


def test_local_lease_expiry_detaches(make_binding: Any) -> None:
    client = _client(make_binding)
    client.tick(T0)
    client.on_open_response(_accept(), T0 + 1_000)
    client.on_heartbeat_failed(T0 + 2_000)
    client.tick(T0 + 2_100_000)  # past the lease ttl measured from our send time
    assert not client.connected
    assert client.runtime.last_reason is ReasonCode.LEASE_EXPIRED


def test_heartbeat_rejection_detaches(make_binding: Any) -> None:
    client = _client(make_binding)
    client.tick(T0)
    client.on_open_response(_accept(), T0 + 1)
    client.tick(T0 + 2)
    client.on_heartbeat_response(
        dataplane_pb2.HeartbeatResponse(rejected=common_pb2.Reason(code="SESSION_UNKNOWN")), T0 + 3
    )
    assert not client.connected


def _rejected_on_the_stream(code: str, session_id: str = "ses_1") -> dataplane_pb2.GatewayMessage:
    return dataplane_pb2.GatewayMessage(
        rejected=dataplane_pb2.RequestRejected(
            envelope=common_pb2.Envelope(session_id=session_id),
            reason=common_pb2.Reason(code=code),
        )
    )


@pytest.mark.parametrize(
    "code",
    [
        "SESSION_UNKNOWN",  # the gateway restarted and no longer knows the session
        "DEPLOYMENT_NOT_ACTIVE",  # the robot's revision is no longer served here
        "AUTH_DEVICE_REVOKED",  # the robot's credential was revoked
        "LEASE_EXPIRED",
        "LEASE_GENERATION_STALE",
    ],
)
def test_a_rejection_that_ends_the_session_detaches_at_once_on_any_channel(
    make_binding: Any, code: str
) -> None:
    stream = _client(make_binding)
    stream.tick(T0)
    stream.on_open_response(_accept(), T0 + 1)
    stream.on_gateway_message(_rejected_on_the_stream(code), T0 + 2)
    assert not stream.connected
    assert stream.runtime.last_reason is ReasonCode(code)

    control = _client(make_binding)
    control.tick(T0)
    control.on_open_response(_accept(), T0 + 1)
    control.on_control_response(
        dataplane_pb2.ControlResponse(rejected=common_pb2.Reason(code=code)), T0 + 2
    )
    assert not control.connected


def test_a_rejected_request_keeps_the_session(make_binding: Any) -> None:
    client = _client(make_binding)
    client.tick(T0)
    client.on_open_response(_accept(), T0 + 1)
    client.on_gateway_message(_rejected_on_the_stream("REQUEST_EXPIRED"), T0 + 2)
    client.on_gateway_message(_rejected_on_the_stream("SESSION_UNKNOWN", "ses_old"), T0 + 3)
    assert client.connected  # a request failed, or an answer to an earlier session arrived


def test_arming_emits_epoch_announcement(make_binding: Any) -> None:
    client = _client(make_binding)
    client.tick(T0)
    client.on_open_response(_accept(), T0 + 1)
    client.runtime.arm(T0 + 2)
    controls = [o for o in client.drain() if o.channel == "control"]
    assert len(controls) == 1
    message = controls[0].message
    assert isinstance(message, dataplane_pb2.ControlRequest)
    assert message.WhichOneof("command") == "epoch_advanced"
    assert message.envelope.task_epoch == 1


def test_rtt_estimator_is_pessimistic_until_enough_samples() -> None:
    estimator = RttEstimator(initial_us=20_000, window_us=1_000_000)
    estimator.add(0, 1_000)
    assert estimator.p99_us() == 20_000
    for i in range(20):
        estimator.add(i, 1_000)
    assert estimator.p99_us() == 1_000


def test_rtt_spike_ages_out_of_the_window_and_recent_recovers_fast() -> None:
    estimator = RttEstimator(initial_us=20_000, window_us=1_000_000)
    for i in range(10):
        estimator.add(i * 10_000, 700_000)  # a spike
    for i in range(5):
        estimator.add(200_000 + i * 10_000, 1_000)
    assert estimator.recent_us() == 1_000  # follows the link immediately
    assert estimator.p99_us() >= 700_000  # planning stays conservative while the spike is recent
    for i in range(10):
        estimator.add(2_000_000 + i * 10_000, 1_000)
    assert estimator.p99_us() == 1_000  # and recovers once it leaves the window
