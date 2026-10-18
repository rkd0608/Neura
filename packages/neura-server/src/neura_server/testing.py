"""Helpers for tests of the gateway core: a core with one deployment, and a robot whose runtime
and session client talk to it directly (no network)."""

from __future__ import annotations

from typing import Any

from neura.v1 import dataplane_pb2
from neura_runtime.binding import ExecutionBinding
from neura_runtime.runtime import RobotRuntime
from neura_runtime.session import SessionClient, SessionConfig
from neura_runtime.testing import FakeController, FakeSensors
from neura_server.admission import AdmissionController
from neura_server.gateway import BatchGroup, Deployment, GatewayCore
from neura_server.leases import LeaseTable, MemoryGenerationStore
from neura_server.profiles import LatencyProfile, ProfilePoint
from neura_server.scheduler import Scheduler

MS = 1_000
GW0 = 9_000_000_000  # gateway clock
R0 = 3_000_000  # robot clock


def make_core(binding: ExecutionBinding, robots: list[str], **kwargs: Any) -> GatewayCore:
    deployment = Deployment("rev_1", binding, frozenset(robots))
    profile = LatencyProfile(
        [ProfilePoint(1, 10 * MS, 15 * MS, 100), ProfilePoint(8, 20 * MS, 30 * MS, 400)]
    )
    group = BatchGroup(
        deployment.group_key,
        Scheduler(profile, max_batch=8),
        AdmissionController(profile, max_batch=8, reserve_fraction=kwargs.pop("reserve", 0.2)),
    )
    return GatewayCore(
        deployments=[deployment],
        groups={deployment.group_key: group},
        leases=LeaseTable(MemoryGenerationStore(), ttl_us=2_000_000),
        **kwargs,
    )


class Robot:
    def __init__(self, binding: ExecutionBinding, name: str, revision_id: str = "rev_1") -> None:
        self.runtime = RobotRuntime(binding, FakeController(), FakeSensors(binding))
        self.client = SessionClient(
            self.runtime, SessionConfig(robot_id=name, revision_id=revision_id)
        )
        self.name = name
        self.now = R0

    def open(self, core: GatewayCore, now_gw: int) -> dataplane_pb2.OpenSessionResponse:
        out = self.client.tick(self.now)
        request = next(o.message for o in out if o.channel == "open")
        assert isinstance(request, dataplane_pb2.OpenSessionRequest)
        response = core.open_session(self.name, request, now_gw)
        self.client.on_open_response(response, self.now)
        return response

    def observation(self) -> dataplane_pb2.RobotMessage:
        for _ in range(3):
            self.now += 33_333
            for item in self.client.tick(self.now):
                if item.channel == "stream":
                    assert isinstance(item.message, dataplane_pb2.RobotMessage)
                    return item.message
        raise AssertionError("no observation")

    def deliver(self, deliveries: list[Any]) -> None:
        for d in deliveries:
            self.client.on_gateway_message(d.message, self.now)
