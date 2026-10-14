"""The gateway process: gRPC adapter, batch dispatchers, lease sweeper and local status API.

All ``GatewayCore`` calls run on the asyncio event-loop thread, so the core needs no locks. Model
inference runs in worker threads; results are applied back on the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import grpc
import numpy as np
import uvicorn
from fastapi import FastAPI
from numpy.typing import NDArray

from neura.v1 import dataplane_pb2, dataplane_pb2_grpc
from neura_server.admission import AdmissionController
from neura_server.config import DeviceRegistry, GatewayConfig, load_binding
from neura_server.gateway import BatchGroup, Delivery, Deployment, GatewayCore
from neura_server.inputs import ModelInputs
from neura_server.leases import (
    GenerationStore,
    LeaseTable,
    MemoryGenerationStore,
    SqliteGenerationStore,
)
from neura_server.policy import MockPolicy, PolicyBackend, PolicyError
from neura_server.profiles import LatencyProfile
from neura_server.scheduler import Scheduler

log = logging.getLogger("neura.gateway")
STREAM_QUEUE = 256


def monotonic_us() -> int:
    return time.monotonic_ns() // 1000


class SimulatedLatency:
    """Wraps a backend and sleeps for a latency sampled from the profile (development only)."""

    def __init__(self, backend: PolicyBackend, profile: LatencyProfile, seed: int = 0) -> None:
        self._backend = backend
        self._profile = profile
        self._rng = random.Random(seed)  # noqa: S311 - latency emulation, not security
        self.chunk_length = backend.chunk_length
        self.action_dim = backend.action_dim

    def infer(self, batch: Sequence[ModelInputs]) -> list[NDArray[np.float32]]:
        p50 = self._profile.p50_us(len(batch))
        p99 = self._profile.p99_us(len(batch))
        sigma = min(0.35, math.log(max(p99, p50 + 1) / p50) / 2.326)
        started = time.perf_counter()
        outputs = self._backend.infer(batch)
        remaining = p50 * math.exp(self._rng.gauss(0.0, sigma)) / 1e6 - (
            time.perf_counter() - started
        )
        if remaining > 0:
            time.sleep(remaining)
        return outputs


@dataclass(slots=True)
class _Group:
    key: str
    backend: PolicyBackend
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class GatewayServer:
    def __init__(
        self, config: GatewayConfig, *, generation_store: GenerationStore | None = None
    ) -> None:
        self.config = config
        self.devices = DeviceRegistry(config.devices)
        deployments, groups, backends = self._build(config, set())
        # A store the server opens itself is closed by stop(); a caller's store stays the caller's.
        self._owned_store: SqliteGenerationStore | None = None
        if generation_store is None:
            if config.lease_store:
                generation_store = self._owned_store = SqliteGenerationStore(config.lease_store)
            else:
                generation_store = MemoryGenerationStore()
        self.core = GatewayCore(
            deployments=deployments,
            groups=groups,
            leases=LeaseTable(generation_store, config.lease_ttl_ms * 1000),
            max_message_bytes=config.max_message_bytes,
            default_rtt_us=int(config.default_rtt_ms * 1000),
        )
        self._groups = {key: _Group(key, backend) for key, backend in backends.items()}
        self._streams: dict[str, asyncio.Queue[dataplane_pb2.GatewayMessage]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._server: grpc.aio.Server | None = None
        self._status_server: uvicorn.Server | None = None
        self.bound_port: int | None = None
        self.started_at = time.time()

    @staticmethod
    def _build(
        config: GatewayConfig, existing: set[str]
    ) -> tuple[list[Deployment], dict[str, BatchGroup], dict[str, PolicyBackend]]:
        """Deployments from config, plus batch groups and backends for group keys not yet known."""
        serving = config.serving
        deployments = []
        groups: dict[str, BatchGroup] = {}
        backends: dict[str, PolicyBackend] = {}
        for item in config.deployments:
            deployment = Deployment(item.revision_id, load_binding(item), frozenset(item.robots))
            deployments.append(deployment)
            key = deployment.group_key
            if key in existing or key in groups:
                continue
            profile = LatencyProfile(serving.profile_points())
            groups[key] = BatchGroup(
                key,
                Scheduler(profile, policy=serving.policy, max_batch=serving.max_batch),
                AdmissionController(
                    profile,
                    max_batch=serving.max_batch,
                    reserve_fraction=serving.reserve_fraction,
                    memory_available_mib=serving.memory_available_mib,
                ),
            )
            backend: PolicyBackend = MockPolicy(deployment.binding)
            if serving.simulate_latency:
                backend = SimulatedLatency(backend, profile)
            backends[key] = backend
        return deployments, groups, backends

    def reload(self, config: GatewayConfig) -> list[str]:
        """Apply a new configuration in place (devices and deployments). Serving settings and
        listen addresses need a restart. Returns the ids of sessions that were closed."""
        deployments, groups, backends = self._build(config, set(self.core.groups))
        self.devices = DeviceRegistry(config.devices)
        closed = self.core.replace_deployments(deployments, groups)
        for key, backend in backends.items():
            group = _Group(key, backend)
            self._groups[key] = group
            if self._server is not None:
                self._tasks.append(
                    asyncio.create_task(self._dispatch(group), name=f"dispatch-{key[:16]}")
                )
        for session_id in closed:
            queue = self._streams.pop(session_id, None)
            if queue is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(_closed_message(session_id))
        self.config = config
        log.info(
            "configuration reloaded: %d deployments, %d sessions closed",
            len(deployments),
            len(closed),
        )
        return closed

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        options = [
            ("grpc.max_receive_message_length", self.config.max_message_bytes),
            ("grpc.max_send_message_length", self.config.max_message_bytes),
        ]
        self._server = grpc.aio.server(options=options)
        dataplane_pb2_grpc.add_RobotGatewayServicer_to_server(_Servicer(self), self._server)
        self.bound_port = self._server.add_insecure_port(self.config.listen)
        await self._server.start()
        for group in self._groups.values():
            self._tasks.append(
                asyncio.create_task(self._dispatch(group), name=f"dispatch-{group.key[:16]}")
            )
        self._tasks.append(asyncio.create_task(self._sweep(), name="lease-sweeper"))
        if self.config.status_listen:
            host, _, port = self.config.status_listen.rpartition(":")
            self._status_server = uvicorn.Server(
                uvicorn.Config(self.status_app(), host=host, port=int(port), log_level="warning")
            )
            self._tasks.append(asyncio.create_task(self._status_server.serve(), name="status-http"))
        log.info("gateway listening on %s (port %s)", self.config.listen, self.bound_port)

    async def stop(self) -> None:
        if self._status_server is not None:
            self._status_server.should_exit = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            await self._server.stop(grace=0.5)
        if self._owned_store is not None:
            self._owned_store.close()
            self._owned_store = None

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        await self._server.wait_for_termination()

    # --- routing ------------------------------------------------------------------------------

    def route(self, deliveries: list[Delivery]) -> None:
        for delivery in deliveries:
            queue = self._streams.get(delivery.session_id)
            if queue is None:
                continue
            try:
                queue.put_nowait(delivery.message)
            except asyncio.QueueFull:
                log.warning("stream for %s is not draining; dropping message", delivery.session_id)

    def wake(self, session_id: str) -> None:
        session = self.core.sessions.get(session_id)
        if session is not None:
            self._groups[session.group.key].wake.set()

    def bind_stream(
        self, session_id: str, queue: asyncio.Queue[dataplane_pb2.GatewayMessage]
    ) -> None:
        self._streams[session_id] = queue

    def has_stream(self, session_id: str) -> bool:
        return session_id in self._streams

    def unbind_stream(self, queue: asyncio.Queue[dataplane_pb2.GatewayMessage]) -> None:
        for session_id in [s for s, q in self._streams.items() if q is queue]:
            del self._streams[session_id]

    async def _dispatch(self, group: _Group) -> None:
        while True:
            await group.wake.wait()
            group.wake.clear()
            while True:
                batch, dropped = self.core.next_batch(group.key, monotonic_us())
                self.route(dropped)
                if batch is None:
                    break
                inputs = [r.inputs for r in batch.requests]
                outputs: list[NDArray[np.float32]] | PolicyError
                try:
                    outputs = await asyncio.to_thread(group.backend.infer, inputs)
                except PolicyError as exc:
                    outputs = exc
                except MemoryError as exc:
                    outputs = PolicyError(str(exc), out_of_memory=True)
                except Exception as exc:
                    log.exception("policy backend failed")
                    outputs = PolicyError(f"{type(exc).__name__}: {exc}")
                self.route(self.core.complete(group.key, batch, outputs, monotonic_us()))

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            self.core.sweep(monotonic_us())
            for group in self._groups.values():
                group.wake.set()  # lets the scheduler expire stale requests promptly

    # --- status -------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        status = self.core.status()
        status["uptime_s"] = round(time.time() - self.started_at, 1)
        status["streams"] = len(self._streams)
        return status

    def status_app(self) -> FastAPI:
        app = FastAPI(
            title="Neura gateway (local status)", docs_url=None, redoc_url=None, openapi_url=None
        )

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/v1/local/dataplane")
        async def dataplane() -> dict[str, Any]:
            return self.status()

        return app


def _closed_message(session_id: str) -> dataplane_pb2.GatewayMessage:
    from neura.v1 import common_pb2

    return dataplane_pb2.GatewayMessage(
        rejected=dataplane_pb2.RequestRejected(
            envelope=common_pb2.Envelope(session_id=session_id),
            reason=common_pb2.Reason(code="LEASE_EXPIRED", message="revision no longer active"),
        )
    )


class _Servicer(dataplane_pb2_grpc.RobotGatewayServicer):  # type: ignore[misc]
    def __init__(self, server: GatewayServer) -> None:
        self._server = server

    async def _robot(self, context: grpc.aio.ServicerContext[Any, Any]) -> str:
        metadata = dict(context.invocation_metadata() or ())
        header = str(metadata.get("authorization", ""))
        token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        robot_id = self._server.devices.authenticate(token) if token else None
        if robot_id is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "AUTH_DEVICE_UNKNOWN")
            raise AssertionError("unreachable")
        return robot_id

    async def OpenSession(  # noqa: N802 - gRPC method name
        self, request: dataplane_pb2.OpenSessionRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> dataplane_pb2.OpenSessionResponse:
        robot_id = await self._robot(context)
        return self._server.core.open_session(robot_id, request, monotonic_us())

    async def Heartbeat(  # noqa: N802
        self, request: dataplane_pb2.HeartbeatRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> dataplane_pb2.HeartbeatResponse:
        robot_id = await self._robot(context)
        return self._server.core.heartbeat(robot_id, request, monotonic_us())

    async def Control(  # noqa: N802
        self, request: dataplane_pb2.ControlRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> dataplane_pb2.ControlResponse:
        robot_id = await self._robot(context)
        return self._server.core.control(robot_id, request, monotonic_us())

    async def Stream(  # noqa: N802
        self,
        request_iterator: AsyncIterator[dataplane_pb2.RobotMessage],
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> AsyncIterator[dataplane_pb2.GatewayMessage]:
        robot_id = await self._robot(context)
        server = self._server
        outbox: asyncio.Queue[dataplane_pb2.GatewayMessage] = asyncio.Queue(maxsize=STREAM_QUEUE)

        async def read() -> None:
            async for message in request_iterator:
                env = message.observation.envelope
                session = server.core.sessions.get(env.session_id)
                if session is not None and session.lease.robot_id == robot_id:
                    server.bind_stream(env.session_id, outbox)
                deliveries = server.core.receive(
                    robot_id, message, message.ByteSize(), monotonic_us()
                )
                server.route(deliveries)
                # Rejections for unknown sessions still reach this stream.
                for delivery in deliveries:
                    if not server.has_stream(delivery.session_id):
                        with contextlib.suppress(asyncio.QueueFull):
                            outbox.put_nowait(delivery.message)
                server.wake(env.session_id)

        reader = asyncio.create_task(read())
        try:
            while True:
                getter = asyncio.create_task(outbox.get())
                done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    continue
                getter.cancel()
                break
        finally:
            reader.cancel()
            server.unbind_stream(outbox)
