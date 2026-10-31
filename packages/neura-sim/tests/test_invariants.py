"""The invariant monitor catches what it claims to catch.

Each scenario breaks one safety rule on purpose, by patching a correct component into a faulty
one, and requires the monitor to report it. Without these, a monitor that never fires would look
the same as a system that never fails.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import pytest

from neura.v1 import common_pb2, dataplane_pb2
from neura_runtime import tensors
from neura_runtime.action_queue import ActionQueue
from neura_runtime.runtime import RobotRuntime, RuntimeState, _Pending
from neura_server.leases import Lease, LeaseTable, MemoryGenerationStore
from neura_sim import invariants
from neura_sim.invariants import InvariantMonitor
from neura_sim.network import LinkModel, Window
from neura_sim.site import MS, SECOND, SimSite, SiteConfig
from neura_sim.soak import Soak, SoakConfig


@pytest.fixture
def config(make_binding: Any) -> SiteConfig:
    return SiteConfig(binding=make_binding(), robots=2)


def test_correct_system_has_no_violations(config: SiteConfig) -> None:
    site = SimSite(config)
    site.run(10 * SECOND)
    assert site.monitor is not None
    assert site.monitor.violations == 0
    assert site.monitor.checked["chunks_accepted"] > 0
    assert site.monitor.checked["actions_dispatched"] > 500


def test_flags_a_runtime_that_accepts_duplicates(
    config: SiteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RobotRuntime.on_chunk

    def forgetful(self: RobotRuntime, chunk: dataplane_pb2.ActionChunk, now_us: int) -> bool:
        env = chunk.envelope
        pending = self._pending
        if pending is None or pending.sequence != env.sequence:
            # The bug: treat any chunk as the answer to the outstanding request.
            self._pending = _Pending(env.sequence, chunk.start_index, now_us, now_us)
        return original(self, chunk, now_us)

    monkeypatch.setattr(RobotRuntime, "on_chunk", forgetful)
    config.downlink = LinkModel(duplicate_probability=0.3)
    site = SimSite(config)
    site.run(5 * SECOND)
    assert site.monitor is not None
    assert site.monitor.counts[invariants.CHUNK_DUPLICATE] > 0


def test_flags_a_fault_cleared_by_reconnecting(
    config: SiteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RobotRuntime.attach_session

    def clearing(self: RobotRuntime, session_id: str, lease_generation: int) -> None:
        if self.state is RuntimeState.FAULTED:  # the bug: a new session clears the latch
            self.fault = None
            self.state = RuntimeState.READY
        original(self, session_id, lease_generation)

    monkeypatch.setattr(RobotRuntime, "attach_session", clearing)
    config.uplink = LinkModel(partitions=[Window(2 * SECOND, 6 * SECOND)])
    site = SimSite(config)
    site.run(10 * SECOND)
    assert site.monitor is not None
    assert site.monitor.counts[invariants.FAULT_CLEARED] > 0


def test_flags_an_action_dispatched_out_of_order(
    config: SiteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ActionQueue.pop
    calls = {"n": 0}

    def skipping(self: ActionQueue) -> tuple[int, Any] | None:
        calls["n"] += 1
        if calls["n"] == 100 and len(self) > 1:
            original(self)  # the bug: one action silently skipped
        return original(self)

    monkeypatch.setattr(ActionQueue, "pop", skipping)
    site = SimSite(config)
    site.run(5 * SECOND)
    assert site.monitor is not None
    assert site.monitor.counts[invariants.DISPATCH_ORDER] == 1


def test_flags_a_stale_chunk_accepted(config: SiteConfig) -> None:
    # A slow downlink delivers every chunk long after its observation was taken.
    config.downlink = LinkModel(spikes=[(Window(3 * SECOND, 4 * SECOND), 300 * MS)])
    site = SimSite(config)
    for robot in site.robots:
        # The bug: a runtime configured with no freshness limit at all.
        robot.runtime._freshness = robot.runtime._freshness.model_copy(
            update={
                "max_observation_age_at_acceptance_us": 10**12,
                "request_timeout_us": 10**12,
            }
        )
    site.run(6 * SECOND)
    assert site.monitor is not None
    assert site.monitor.counts[invariants.CHUNK_STALE] > 0


def test_flags_an_action_that_no_chunk_produced(
    config: SiteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ActionQueue.pop
    calls = {"n": 0}

    def tampering(self: ActionQueue) -> tuple[int, Any] | None:
        item = original(self)
        calls["n"] += 1
        if item is not None and calls["n"] == 50:
            index, action = item
            return index, action * 0.5  # the bug: the action is not what the model produced
        return item

    monkeypatch.setattr(ActionQueue, "pop", tampering)
    site = SimSite(config)
    site.run(4 * SECOND)
    assert site.monitor is not None
    assert site.monitor.counts[invariants.DISPATCH_VALUE] == 1


# --- gateway-side rules, fed directly -----------------------------------------------------------


def _chunk(monitor: InvariantMonitor, session: str, generation: int) -> dataplane_pb2.ActionChunk:
    binding = monitor.binding
    assert binding is not None
    actions = tensors.encode(np.zeros((binding.chunk_length, binding.action_dim), dtype=np.float32))
    return dataplane_pb2.ActionChunk(
        envelope=common_pb2.Envelope(
            session_id=session,
            lease_generation=generation,
            task_epoch=1,
            sequence=1,
            artifact_digest=binding.artifact_digest,
            execution_profile_digest=binding.execution_profile_digest,
        ),
        start_index=0,
        actions=actions,
        actions_digest=common_pb2.PayloadDigest(sha256=tensors.payload_digest(actions.data)),
    )


def test_flags_a_chunk_emitted_without_a_live_lease(make_binding: Any) -> None:
    monitor = InvariantMonitor(make_binding())
    leases = LeaseTable(MemoryGenerationStore(), ttl_us=1_000)
    lease = leases.acquire("robot-0", now_us=0)
    assert isinstance(lease, Lease)
    monitor.lease_granted("robot-0", lease.session_id, lease.generation)
    chunk = _chunk(monitor, lease.session_id, lease.generation)
    monitor.chunk_emitted(chunk, leases, gateway_now_us=500)
    assert monitor.violations == 0
    monitor.chunk_emitted(chunk, leases, gateway_now_us=1_000)  # expired
    assert monitor.counts[invariants.EMIT_UNLEASED] == 1


def test_flags_lease_generations_that_do_not_increase(make_binding: Any) -> None:
    monitor = InvariantMonitor(make_binding())
    monitor.lease_granted("robot-0", "ses_a", 3)
    monitor.lease_granted("robot-0", "ses_b", 3)
    assert monitor.counts[invariants.LEASE_NOT_MONOTONIC] == 1


def test_flags_a_chunk_accepted_under_a_lease_never_granted_to_the_client(
    make_binding: Any,
) -> None:
    monitor = InvariantMonitor(make_binding())
    client = monitor.client(object(), "robot-0")
    chunk = _chunk(monitor, "ses_other", 7)
    observation = dataplane_pb2.Observation(
        envelope=chunk.envelope,
        anchor_index=0,
        capture_time_us=1_000,
    )
    monitor.observation_sent(client, observation)
    arrival = monitor.chunk_arriving(
        client,
        chunk,
        session_id="ses_other",
        lease_generation=7,
        task_epoch=1,
        state=RuntimeState.ARMED,
        accepted_before=0,
        now_us=2_000,
    )
    monitor.chunk_handled(arrival, accepted_after=1, state=RuntimeState.RUNNING)
    assert monitor.counts[invariants.CHUNK_NOT_GRANTED] == 1
    assert monitor.counts[invariants.CHUNK_NOT_AUTHORIZED] == 1
    assert monitor.violations == 2


# --- the soak ------------------------------------------------------------------------------------


def test_short_soak_with_dense_faults_passes(make_binding: Any) -> None:
    """Five simulated minutes with a fault every two seconds on average (CI's short soak; the
    72-hour soak runs in the soak workflow)."""
    soak = Soak(
        make_binding(),
        SoakConfig(hours=5 / 60, robots=3, seed=11, mean_fault_interval_s=2.0, cooldown_s=40),
    )
    report = soak.run()
    assert report["invariants"]["violations"] == 0, report["invariants"]
    assert report["findings"] == [], report["findings"]
    assert report["passed"]
    assert report["faults_total"] > 100
    assert len(report["faults_injected"]) >= 15  # most of the catalog was exercised
    assert report["recoveries"] > 0
    # Enough chunks were checked for the invariants to mean something. Faults, resets and
    # robots' timing share one random stream, so any change in behavior gives another trajectory:
    # across seeds 1-40 this soak accepts 270 to 540 chunks.
    assert report["invariants"]["checked"]["chunks_accepted"] > 250


def test_soak_is_reproducible_from_its_seed(make_binding: Any) -> None:
    config = SoakConfig(hours=1 / 60, robots=2, seed=5, mean_fault_interval_s=3.0, cooldown_s=10)
    a = Soak(make_binding(), config).run()
    b = Soak(make_binding(), dataclasses.replace(config)).run()
    for report in (a, b):
        report.pop("wall_seconds")
        for hour in report["hours"]:
            hour.pop("rss_mib")
    assert a == b
