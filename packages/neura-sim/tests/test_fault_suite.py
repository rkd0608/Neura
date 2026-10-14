"""The data-plane fault suite (design §7), run in the deterministic site simulator.

Every scenario also checks the invariants that must hold no matter what happens:
  - within a task epoch, dispatched action indexes are exactly 0, 1, 2, ... (no duplicate, no
    skip, no re-dispatch), and epochs never go backwards;
  - nothing out of the robot's declared ranges is ever sent (the simulated controller raises);
  - a latched fault is never cleared without an explicit reset.
"""

from __future__ import annotations

from typing import Any

import pytest

from neura_contracts import FaultResponse, ReasonCode
from neura_runtime.runtime import RuntimeState
from neura_server.scheduler import Policy
from neura_sim.network import LinkModel, Window
from neura_sim.site import MS, SECOND, SimRobot, SimSite, SiteConfig, WorkerFaults


def assert_invariants(site: SimSite) -> None:
    for robot in [*site.robots, *site.extra_clients]:
        previous_epoch = 0
        next_index: dict[int, int] = {}
        for epoch, index in robot.dispatches:
            assert epoch >= previous_epoch, f"{robot.name}: epoch went backwards"
            expected = next_index.get(epoch, 0)
            assert index == expected, (
                f"{robot.name}: epoch {epoch} dispatched {index}, expected {expected}"
            )
            next_index[epoch] = index + 1
            previous_epoch = epoch


def run(site: SimSite, seconds: float) -> Any:
    report = site.run(int(seconds * SECOND))
    assert_invariants(site)
    return report


@pytest.fixture
def config(make_binding: Any) -> SiteConfig:
    return SiteConfig(binding=make_binding())


def states(site: SimSite) -> set[RuntimeState]:
    return {r.runtime.state for r in site.robots}


# --- nominal operation -------------------------------------------------------------------------


@pytest.mark.parametrize("policy", list(Policy))
def test_nominal_shared_serving(config: SiteConfig, policy: Policy) -> None:
    config.robots = 6
    config.policy = policy
    site = SimSite(config)
    report = run(site, 20)
    assert states(site) == {RuntimeState.RUNNING}
    for robot in report.robots.values():
        assert robot.fault is None
        assert robot.starvations == 0
        assert robot.dispatched > 550  # ~600 periods in 20 s at 30 Hz
    group = report.group()
    assert group["deadline_misses"] == 0
    assert max(group["batch_sizes"]) > 1, "requests from different robots should share batches"


def test_clocks_are_never_compared_across_machines(config: SiteConfig) -> None:
    config.robots = 3
    config.gateway_clock_offset_us = -5_000_000_000
    config.robot_clock_offsets_us = [0, 86_400 * SECOND * 365, 123_456_789_012]
    site = SimSite(config)
    run(site, 10)
    assert states(site) == {RuntimeState.RUNNING}


def test_simulation_is_deterministic(config: SiteConfig) -> None:
    config.uplink = LinkModel(base_us=800, jitter_us=3_000)
    a = SimSite(config).run(5 * SECOND)
    b = SimSite(config).run(5 * SECOND)
    assert a.robots == b.robots
    assert a.gateway["groups"] == b.gateway["groups"]


def test_admission_refuses_sessions_it_cannot_serve(config: SiteConfig) -> None:
    # With the example's 100 ms dispatch-freshness limit, a request that arrives just after a
    # 45 ms batch started must be dispatched within the next batch, so one worker admits only as
    # many sessions as fit in one batch behind a running one.
    config.robots = 40
    site = SimSite(config)
    report = run(site, 10)
    running = [r for r in site.robots if r.runtime.state is RuntimeState.RUNNING]
    refused = [
        r for r in site.robots if r.client.last_rejection is ReasonCode.ADMISSION_CAPACITY_EXCEEDED
    ]
    assert len(running) == config.max_batch
    assert len(refused) == 40 - config.max_batch
    for robot in running:
        assert report.robots[robot.name].starvations == 0


# --- network -----------------------------------------------------------------------------------


def test_short_delay_spike_starves_then_recovers(config: SiteConfig) -> None:
    config.robots = 2
    spike = (Window(5 * SECOND, int(5.8 * SECOND)), 700 * MS)
    config.uplink = LinkModel(spikes=[spike])
    site = SimSite(config)
    report = run(site, 12)
    assert states(site) == {RuntimeState.RUNNING}
    assert any(r.starvations for r in report.robots.values())
    assert all(r.fault is None for r in report.robots.values())


def test_long_outage_faults_and_stays_faulted_after_recovery(config: SiteConfig) -> None:
    config.robots = 2
    config.uplink = LinkModel(partitions=[Window(5 * SECOND, 8 * SECOND)])
    site = SimSite(config)
    report = run(site, 15)
    for robot in report.robots.values():
        assert robot.fault is ReasonCode.STARVATION_GRACE_EXHAUSTED
        assert robot.state is RuntimeState.FAULTED
    # The network came back; motion must not restart without an explicit local reset.
    dispatched = {r.name: r.runtime.dispatched for r in site.robots}
    run(site, 3)
    assert {r.name: r.runtime.dispatched for r in site.robots} == dispatched


def test_reset_after_outage_resumes_under_a_new_lease(config: SiteConfig) -> None:
    config.robots = 1
    config.uplink = LinkModel(partitions=[Window(3 * SECOND, 7 * SECOND)])
    site = SimSite(config)
    run(site, 10)
    robot = site.robots[0]
    generation = robot.runtime.lease_generation
    assert robot.runtime.state is RuntimeState.FAULTED
    robot.runtime.reset(robot.now(), revalidated=True)
    run(site, 3)
    assert robot.runtime.snapshot().state is RuntimeState.RUNNING
    assert generation is not None
    assert robot.runtime.lease_generation is not None
    assert robot.runtime.lease_generation >= generation


def test_jitter_duplicates_and_reordering_never_break_invariants(config: SiteConfig) -> None:
    config.robots = 4
    noisy = LinkModel(
        base_us=1_000,
        jitter_us=8_000,
        duplicate_probability=0.2,
        reorder_probability=0.1,
        reorder_extra_us=30_000,
    )
    config.uplink = noisy
    config.downlink = noisy
    site = SimSite(config)
    report = run(site, 20)
    assert states(site) == {RuntimeState.RUNNING}
    duplicates = sum(
        r.counters.get(ReasonCode.CHUNK_DUPLICATE.value, 0) for r in report.robots.values()
    )
    assert duplicates > 0


# --- workers -----------------------------------------------------------------------------------


def test_worker_crash_never_replays_old_results(config: SiteConfig) -> None:
    config.robots = 4
    config.worker_faults = WorkerFaults(crash_at_us=[3 * SECOND, 6 * SECOND], restart_us=300 * MS)
    site = SimSite(config)
    report = run(site, 12)
    assert states(site) == {RuntimeState.RUNNING}
    assert all(r.fault is None for r in report.robots.values())


def test_worker_restart_longer_than_grace_faults_robots(config: SiteConfig) -> None:
    config.robots = 2
    config.worker_faults = WorkerFaults(crash_at_us=[3 * SECOND], restart_us=3 * SECOND)
    site = SimSite(config)
    report = run(site, 8)
    assert {r.fault for r in report.robots.values()} == {ReasonCode.STARVATION_GRACE_EXHAUSTED}


@pytest.mark.parametrize(
    ("faults", "reason"),
    [
        (WorkerFaults(out_of_memory_batches={5, 6}), ReasonCode.WORKER_OUT_OF_MEMORY),
        (WorkerFaults(invalid_output_batches={5, 6}), ReasonCode.WORKER_OUTPUT_INVALID),
    ],
)
def test_bad_batches_are_rejected_and_robots_continue(
    config: SiteConfig, faults: WorkerFaults, reason: ReasonCode
) -> None:
    config.robots = 3
    config.worker_faults = faults
    site = SimSite(config)
    report = run(site, 10)
    assert states(site) == {RuntimeState.RUNNING}
    assert sum(r.counters.get(reason.value, 0) for r in report.robots.values()) > 0


def test_out_of_range_output_latches_fault_and_nothing_unsafe_is_sent(config: SiteConfig) -> None:
    config.robots = 2
    config.worker_faults = WorkerFaults(corrupt_output_batches=set(range(4, 50)))
    site = SimSite(config)
    report = run(site, 6)  # SimController raises if anything out of range is dispatched
    assert {r.fault for r in report.robots.values()} == {ReasonCode.ACTION_OUT_OF_RANGE}


# --- gateway -----------------------------------------------------------------------------------


def test_gateway_restart_fences_old_sessions(config: SiteConfig) -> None:
    config.robots = 3
    site = SimSite(config)
    run(site, 4)
    before = {r.name: r.runtime.lease_generation for r in site.robots}
    site.restart_gateway()
    report = run(site, 8)
    assert states(site) == {RuntimeState.RUNNING}
    for robot in site.robots:
        old, new = before[robot.name], robot.runtime.lease_generation
        assert old is not None
        assert new is not None
        assert new > old
    assert all(r.fault is None for r in report.robots.values())


def test_second_client_for_same_robot_is_refused(config: SiteConfig) -> None:
    config.robots = 1
    site = SimSite(config)
    duplicate = site.add_client("robot-0", offset_us=42, start_at_us=2 * SECOND)
    run(site, 6)
    assert site.robots[0].runtime.state is RuntimeState.RUNNING
    assert duplicate.client.last_rejection is ReasonCode.LEASE_BUSY
    assert duplicate.runtime.dispatched == 0


# --- robot side --------------------------------------------------------------------------------


def _at(site: SimSite, seconds: float, action: Any) -> None:
    site.at(int(seconds * SECOND), action)


def test_missing_camera_starves_and_faults_without_substitution(config: SiteConfig) -> None:
    config.robots = 2
    site = SimSite(config)
    _at(site, 3, lambda: site.robots[0].sensors.missing_cameras.add("wrist"))
    report = run(site, 8)
    assert report.robots["robot-0"].fault is ReasonCode.STARVATION_GRACE_EXHAUSTED
    assert report.robots["robot-0"].counters[ReasonCode.OBSERVATION_INCOMPLETE.value] > 0
    assert report.robots["robot-1"].state is RuntimeState.RUNNING


def test_camera_skew_beyond_limit_is_not_sent(config: SiteConfig) -> None:
    config.robots = 1
    site = SimSite(config)
    _at(site, 3, lambda: site.robots[0].sensors.camera_lag_us.update(front=80 * MS))
    report = run(site, 8)
    assert report.robots["robot-0"].fault is ReasonCode.STARVATION_GRACE_EXHAUSTED


def test_operator_takeover_revokes_policy_authority(config: SiteConfig) -> None:
    config.robots = 2
    config.auto_arm = True
    site = SimSite(config)
    robot = site.robots[0]

    def takeover() -> None:
        robot.auto_arm = False
        robot.runtime.operator_takeover(robot.now())

    _at(site, 4, takeover)
    run(site, 4.001)
    dispatched = robot.runtime.dispatched
    run(site, 4)
    assert robot.runtime.state is RuntimeState.READY
    assert robot.runtime.dispatched == dispatched
    assert site.robots[1].runtime.state is RuntimeState.RUNNING


def test_controller_failure_faults_with_stop_response(config: SiteConfig) -> None:
    config.robots = 1
    site = SimSite(config)
    _at(site, 3, lambda: setattr(site.robots[0].controller, "fail_commands", True))
    report = run(site, 5)
    assert report.robots["robot-0"].fault is ReasonCode.CONTROLLER_COMMAND_FAILED
    assert site.robots[0].controller.log.responses[-1] is FaultResponse.CONTROLLED_STOP


def test_lost_actuator_authority_faults(config: SiteConfig) -> None:
    config.robots = 1
    site = SimSite(config)
    _at(site, 3, lambda: setattr(site.robots[0].controller, "authority", False))
    report = run(site, 5)
    assert report.robots["robot-0"].fault is ReasonCode.ACTUATOR_LOCK_LOST


def test_robot_keeps_executing_validated_actions_while_gateway_is_unreachable(
    config: SiteConfig,
) -> None:
    config.robots = 1
    config.uplink = LinkModel(partitions=[Window(4 * SECOND, int(4.4 * SECOND))])
    site = SimSite(config)
    robot: SimRobot = site.robots[0]
    run(site, 4)
    before = robot.runtime.dispatched
    run(site, 0.4)
    assert robot.runtime.dispatched - before >= 10  # queued actions kept flowing
    run(site, 4)
    assert robot.runtime.state is RuntimeState.RUNNING
