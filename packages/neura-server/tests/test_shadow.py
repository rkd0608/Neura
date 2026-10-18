"""Shadow serving at the gateway (design §28.3): a candidate gets the observations robots send the
running model, its actions are compared and never sent, and it never takes the running model's
place in a scheduler or a forward pass."""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura.v1 import dataplane_pb2
from neura_adapters import MockPolicy, PolicyError
from neura_runtime.binding import ExecutionBinding
from neura_server.gateway import GatewayCore
from neura_server.shadow import PAIR_TIMEOUT_US, ShadowDeployment, comparable, shadow_verdict
from neura_server.testing import GW0, MS, Robot, make_core


def candidate_binding(make_binding: Any) -> ExecutionBinding:
    def edit(artifact: dict[str, Any], robot: dict[str, Any], profile: dict[str, Any]) -> None:
        artifact["metadata"]["version"] = "candidate-1"  # another artifact, same contract

    binding: ExecutionBinding = make_binding(edit=edit)
    return binding


def shadowed_core(make_binding: Any, every_nth: int = 1) -> tuple[GatewayCore, Any, Any]:
    running = make_binding()
    candidate = candidate_binding(make_binding)
    shadow = ShadowDeployment("rev_2", "rev_1", candidate, frozenset({"r1"}), every_nth=every_nth)
    return make_core(running, ["r1"], shadows=[shadow]), running, candidate


def serve_primary(core: GatewayCore, now: int) -> list[Any]:
    key = next(iter(core.groups))
    batch, _ = core.next_batch(key, now)
    assert batch is not None
    binding = next(iter(core.sessions.values())).deployment.binding
    outputs = MockPolicy(binding).infer([r.inputs for r in batch.requests])
    return core.complete(key, batch, outputs, now + 15 * MS)


def observe(robot: Robot, core: GatewayCore, now_gw: int) -> Any:
    """The robot's next observation: it asks again when its queue runs low, and renews its
    lease meanwhile."""
    for _ in range(500):
        robot.now += 33_333
        for item in robot.client.tick(robot.now):
            if item.channel == "stream":
                return item.message
            if item.channel == "heartbeat":
                assert isinstance(item.message, dataplane_pb2.HeartbeatRequest)
                response = core.heartbeat(robot.name, item.message, now_gw)
                robot.client.on_heartbeat_response(response, robot.now)
    raise AssertionError("no observation")


def offset(output: NDArray[np.float32], binding: ExecutionBinding, share: float) -> Any:
    """The same chunk, ``share`` of each command's range away, in model units."""
    transform = binding.action_transform
    robot = transform.to_robot(output.astype(np.float64)) + share * (transform.high - transform.low)
    return transform.to_model(robot).astype(np.float32)


def test_the_candidate_sees_what_the_robot_sees_and_sends_it_nothing(make_binding: Any) -> None:
    core, running, candidate = shadowed_core(make_binding)
    robot = Robot(running, "r1")
    assert robot.open(core, GW0).WhichOneof("result") == "accepted"
    robot.runtime.arm(robot.now)
    message = observe(robot, core, GW0)
    assert core.receive("r1", message, message.ByteSize(), GW0 + MS) == []
    # The candidate is not in the robot's scheduler: one request queued, the robot's.
    group = next(iter(core.groups.values()))
    assert group.scheduler.queued == 1
    request = core.next_shadow("rev_2")
    assert request is not None
    assert core.next_shadow("rev_2") is None  # one at a time
    deliveries = serve_primary(core, GW0 + 2 * MS)
    assert [d.message.WhichOneof("body") for d in deliveries] == ["action_chunk"]
    assert {d.session_id for d in deliveries} == set(core.sessions)
    output = MockPolicy(candidate).infer([request.inputs])[0]
    core.shadow_done("rev_2", request, offset(output, candidate, 0.1))
    stats = core.status()["shadows"]["rev_2"]
    assert stats["of_revision_id"] == "rev_1"
    assert (stats["offered"], stats["compared"], stats["robots"]) == (1, 1, 1)
    assert abs(stats["divergence"]["mean"] - 0.1) < 1e-4  # 10% of each command's range
    robot.deliver(deliveries)
    assert robot.runtime.counters["chunks_accepted"] == 1  # the robot got its own chunk only


def test_an_identical_candidate_does_not_diverge(make_binding: Any) -> None:
    core, running, candidate = shadowed_core(make_binding)
    robot = Robot(running, "r1")
    robot.open(core, GW0)
    robot.runtime.arm(robot.now)
    message = observe(robot, core, GW0)
    core.receive("r1", message, message.ByteSize(), GW0 + MS)
    request = core.next_shadow("rev_2")
    assert request is not None
    # The candidate finishes first; the comparison waits for the running model's chunk.
    core.shadow_done("rev_2", request, MockPolicy(candidate).infer([request.inputs])[0])
    assert core.status()["shadows"]["rev_2"]["compared"] == 0
    serve_primary(core, GW0 + 2 * MS)
    stats = core.status()["shadows"]["rev_2"]
    assert stats["compared"] == 1
    assert stats["divergence"]["mean"] == 0.0


def test_a_busy_candidate_skips_observations_and_never_delays_the_robot(make_binding: Any) -> None:
    core, running, _ = shadowed_core(make_binding)
    robot = Robot(running, "r1")
    robot.open(core, GW0)
    robot.runtime.arm(robot.now)
    now = GW0
    message = observe(robot, core, GW0)
    core.receive("r1", message, message.ByteSize(), now + MS)
    in_flight = core.next_shadow("rev_2")
    assert in_flight is not None
    for _ in range(4):  # the candidate is still working; the robot is served every time
        now += 40 * MS
        robot.deliver(serve_primary(core, now))
        message = observe(robot, core, now)
        assert core.receive("r1", message, message.ByteSize(), now + MS) == []
    stats = core.status()["shadows"]["rev_2"]
    assert stats["skipped"] == {"busy": 4}
    assert robot.runtime.counters["chunks_accepted"] == 4
    core.shadow_done("rev_2", in_flight, PolicyError("worker crashed"))
    assert core.status()["shadows"]["rev_2"]["failed"] == 1


def test_sampling_invalid_outputs_and_lost_pairs(make_binding: Any) -> None:
    core, running, candidate = shadowed_core(make_binding, every_nth=2)
    robot = Robot(running, "r1")
    robot.open(core, GW0)
    robot.runtime.arm(robot.now)
    now = GW0
    taken = []
    for _ in range(4):
        now += 40 * MS
        message = observe(robot, core, now)
        core.receive("r1", message, message.ByteSize(), now)
        request = core.next_shadow("rev_2")
        if request is not None:
            taken.append(request)
            nan = np.full((candidate.chunk_length, candidate.action_dim), np.nan, np.float32)
            core.shadow_done("rev_2", request, nan)
        robot.deliver(serve_primary(core, now + MS))
    stats = core.status()["shadows"]["rev_2"]
    assert len(taken) == stats["offered"] == 2  # every other observation
    assert stats["invalid_outputs"] == 2
    assert stats["compared"] == 0
    # When the running model's chunk never comes (its worker failed), the candidate's result is
    # forgotten, and counted.
    for _ in range(2):  # the next sampled observation
        now += 40 * MS
        message = observe(robot, core, now)
        core.receive("r1", message, message.ByteSize(), now)
        lost = core.next_shadow("rev_2")
        key = next(iter(core.groups))
        batch, _ = core.next_batch(key, now + MS)
        assert batch is not None
        robot.deliver(core.complete(key, batch, PolicyError("worker crashed"), now + 2 * MS))
        if lost is not None:
            break
    assert lost is not None
    core.shadow_done("rev_2", lost, MockPolicy(candidate).infer([lost.inputs])[0])
    core.sweep(now + PAIR_TIMEOUT_US + 1)
    assert core.status()["shadows"]["rev_2"]["skipped"].get("unpaired") == 1


def test_shadows_are_replaced_without_losing_their_statistics(make_binding: Any) -> None:
    core, _, candidate = shadowed_core(make_binding)
    tracker = core.shadows["rev_2"]
    tracker.stats.offered = 7
    same = ShadowDeployment("rev_2", "rev_1", candidate, frozenset({"r1"}))
    core.replace_shadows([same])
    assert core.shadows["rev_2"] is tracker
    other = ShadowDeployment("rev_2", "rev_1", candidate, frozenset({"r1", "r2"}))
    core.replace_shadows([other])
    assert core.shadows["rev_2"].stats.offered == 0
    core.replace_shadows([])
    assert core.status()["shadows"] == {}


def test_what_can_shadow_what(make_binding: Any) -> None:
    running = make_binding()
    assert comparable(candidate_binding(make_binding), running) is None

    def other_robot(
        artifact: dict[str, Any], robot: dict[str, Any], profile: dict[str, Any]
    ) -> None:
        robot["metadata"]["version"] = "another-arm"

    assert "robot profile" in str(comparable(make_binding(edit=other_robot), running))


def test_the_verdict() -> None:
    def stats(**kw: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "compared": 500,
            "invalid_outputs": 0,
            "failed": 0,
            "out_of_range": {},
            "divergence": {"mean": 0.02, "p95": 0.05, "max": 0.1},
        }
        return {**base, **kw}

    assert shadow_verdict(stats(), min_samples=200, tolerance=0.05)[0] == "non_inferior"
    assert shadow_verdict(stats(compared=10), min_samples=200, tolerance=0.05)[0] == "inconclusive"
    assert shadow_verdict(stats(), min_samples=200, tolerance=0.01)[0] == "inferior"
    assert shadow_verdict(stats(invalid_outputs=1), min_samples=200, tolerance=0.05)[0] == (
        "inferior"
    )
    out = stats(out_of_range={"candidate": 3, "running": 0})
    assert shadow_verdict(out, min_samples=200, tolerance=0.05)[0] == "inferior"
