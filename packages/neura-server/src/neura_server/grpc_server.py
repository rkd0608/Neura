"""The gateway process: gRPC adapter, batch dispatchers, lease sweeper and local status API.

Robots' calls arrive through `neura_server.robot_service`. All ``GatewayCore`` calls run on the
asyncio event-loop thread, so the core needs no locks. Model
inference runs in policy worker processes, one per batch group (`neura_server.backends`), or with
the simulated backend in a thread; results are applied back on the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import math
import random
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import grpc
import numpy as np
import uvicorn
import yaml
from fastapi import FastAPI, Response
from numpy.typing import NDArray
from prometheus_client import CONTENT_TYPE_LATEST

from neura.v1 import capture_pb2, dataplane_pb2, dataplane_pb2_grpc
from neura_adapters import MockPolicy, ModelInputs, PolicyBackend, PolicyError
from neura_server.admission import AdmissionController
from neura_server.backends import Backend, InProcessBackend, WorkerBackend, WorkerSpec
from neura_server.config import (
    DeploymentConfig,
    DeviceRegistry,
    GatewayConfig,
    ServingConfig,
    ShadowConfig,
    TlsConfig,
    load_binding,
    load_binding_files,
    load_profile_points,
    profile_batch_sizes,
)
from neura_server.gateway import BatchGroup, Delivery, Deployment, GatewayCore
from neura_server.leases import (
    GenerationStore,
    LeaseTable,
    MemoryGenerationStore,
    SqliteGenerationStore,
)
from neura_server.metrics import GatewayMetrics
from neura_server.profiles import LatencyProfile
from neura_server.robot_service import RobotService, closed_message, monotonic_us
from neura_server.sandbox import launcher_for, remove_stale
from neura_server.scheduler import Batch, Scheduler
from neura_server.shadow import ShadowDeployment

log = logging.getLogger("neura.gateway")
# A shadow's candidate has no deadline (no robot waits for it), but a hung worker is restarted.
SHADOW_TIMEOUT_S = 60.0


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


class EmbeddedServer(uvicorn.Server):
    """A uvicorn server inside the gateway's process, which leaves the process's signals alone.

    uvicorn takes SIGTERM and SIGINT for itself and raises them again once it has stopped, which
    kills the process before the gateway stops its workers and removes their directory. The
    gateway's entry point owns those signals; this server stops when the gateway stops it.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@dataclass(slots=True)
class _Group:
    key: str
    backend: Backend
    profile_source: str
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _Shadow:
    revision_id: str
    identity: tuple[Any, ...]  # what its worker loads, and where
    backend: Backend
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class GatewayServer:
    def __init__(
        self, config: GatewayConfig, *, generation_store: GenerationStore | None = None
    ) -> None:
        self.config = config
        self.devices = DeviceRegistry(config.devices, site_id=config.site_id)
        self._group_seq = 0
        self._owned_worker_dir: Path | None = None
        if config.serving.worker_dir is not None:
            config.serving.worker_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._worker_dir = config.serving.worker_dir
        else:
            self._worker_dir = self._owned_worker_dir = Path(tempfile.mkdtemp(prefix="neura-w-"))
        try:
            deployments, groups, backends = self._build(config, set())
            shadows, self._shadows = self._build_shadows(config, {})
        except BaseException:
            if self._owned_worker_dir is not None:  # nothing will ever stop() this server
                shutil.rmtree(self._owned_worker_dir, ignore_errors=True)
            raise
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
            shadows=shadows,
        )
        self._groups = backends
        self._streams: dict[str, asyncio.Queue[dataplane_pb2.GatewayMessage]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._server: grpc.aio.Server | None = None
        self._status_server: uvicorn.Server | None = None
        self._status_task: asyncio.Task[None] | None = None
        self.bound_port: int | None = None
        self.started_at = time.time()
        self.metrics = GatewayMetrics(
            self.status, config.tls.certificate if config.tls is not None else None
        )

    def _build(
        self, config: GatewayConfig, existing: set[str]
    ) -> tuple[list[Deployment], dict[str, BatchGroup], dict[str, _Group]]:
        """Deployments from config, plus batch groups and their backends for group keys not yet
        known."""
        serving = config.serving
        deployments = []
        groups: dict[str, BatchGroup] = {}
        backends: dict[str, _Group] = {}
        first: dict[str, DeploymentConfig] = {}
        for item in config.deployments:
            deployment = Deployment(
                item.revision_id, load_binding(item), frozenset(item.robots), item.device
            )
            deployments.append(deployment)
            key = deployment.group_key
            if key in first:
                # One worker serves the group, so its deployments must agree on what it loads.
                other = first[key]
                if (item.checkpoint, item.adapter) != (other.checkpoint, other.adapter):
                    raise ValueError(
                        f"deployments {other.revision_id} and {item.revision_id} share batch "
                        f"group {key} but name different checkpoints or adapters"
                    )
                continue
            first[key] = item
            if key in existing:
                continue
            if item.profile is not None:
                points, source = load_profile_points(item.profile), "measured_profile"
            else:
                points, source = serving.profile_points(), "configured_profile"
            profile = LatencyProfile(points)
            groups[key] = BatchGroup(
                key,
                Scheduler(
                    profile,
                    policy=serving.policy,
                    max_batch=serving.max_batch,
                    batch_window_us=int(serving.batch_window_ms * 1000),
                ),
                AdmissionController(
                    profile,
                    max_batch=serving.max_batch,
                    reserve_fraction=serving.reserve_fraction,
                    memory_available_mib=serving.memory_available_mib,
                    batch_window_us=int(serving.batch_window_ms * 1000),
                ),
            )
            backends[key] = _Group(key, self._backend(serving, item, deployment, profile), source)
        return deployments, groups, backends

    def _backend(
        self,
        serving: ServingConfig,
        item: DeploymentConfig,
        deployment: Deployment,
        profile: LatencyProfile,
    ) -> Backend:
        if serving.backend == "simulated":
            policy: PolicyBackend = MockPolicy(deployment.binding)
            if serving.simulate_latency:
                policy = SimulatedLatency(policy, profile)
            return InProcessBackend(policy)
        self._group_seq += 1
        spec = WorkerSpec(
            artifact=item.artifact,
            robot_profile=item.robot_profile,
            execution_profile=item.execution_profile,
            device=deployment.device_name,
            checkpoint=item.checkpoint,
            adapter=item.adapter,
            warmup_batches=tuple(profile_batch_sizes(serving.max_batch)),
        )
        return WorkerBackend(
            f"g{self._group_seq}",
            spec,
            launcher=launcher_for(
                serving.sandbox,
                serving.worker_command,
                deployment.binding.artifact.spec.runtime_image.reference,
            ),
            socket_dir=self._worker_dir,
            start_timeout_s=serving.worker_start_timeout_s,
        )

    def _build_shadows(
        self, config: GatewayConfig, existing: dict[str, _Shadow]
    ) -> tuple[list[ShadowDeployment], dict[str, _Shadow]]:
        """Shadow deployments from config, and their backends: an existing one is kept when its
        worker would load the same thing on the same device."""
        deployments, runners = [], {}
        for item in config.shadows:
            try:
                binding = load_binding_files(
                    item.artifact, item.robot_profile, item.execution_profile
                )
            except ValueError as exc:
                raise ValueError(f"shadow {item.revision_id}: {exc}") from exc
            shadow = ShadowDeployment(
                item.revision_id,
                item.of_revision_id,
                binding,
                frozenset(item.robots),
                item.device,
                item.every_nth,
            )
            deployments.append(shadow)
            identity = (
                binding.artifact_digest,
                binding.execution_profile_digest,
                item.checkpoint,
                item.adapter,
                shadow.device_name,
            )
            kept = existing.get(item.revision_id)
            if kept is not None and kept.identity == identity:
                runners[item.revision_id] = kept
            else:
                runners[item.revision_id] = _Shadow(
                    item.revision_id, identity, self._shadow_backend(config.serving, item, shadow)
                )
        return deployments, runners

    def _shadow_backend(
        self, serving: ServingConfig, item: ShadowConfig, shadow: ShadowDeployment
    ) -> Backend:
        if serving.backend == "simulated":
            policy: PolicyBackend = MockPolicy(shadow.binding)
            if serving.simulate_latency:
                policy = SimulatedLatency(policy, LatencyProfile(serving.profile_points()))
            return InProcessBackend(policy)
        self._group_seq += 1
        spec = WorkerSpec(
            artifact=item.artifact,
            robot_profile=item.robot_profile,
            execution_profile=item.execution_profile,
            device=shadow.device_name,
            checkpoint=item.checkpoint,
            adapter=item.adapter,
            warmup_batches=(1,),
        )
        return WorkerBackend(
            f"s{self._group_seq}",
            spec,
            launcher=launcher_for(
                serving.sandbox,
                serving.worker_command,
                shadow.binding.artifact.spec.runtime_image.reference,
            ),
            socket_dir=self._worker_dir,
            start_timeout_s=serving.worker_start_timeout_s,
        )

    def reload(self, config: GatewayConfig) -> list[str]:
        """Apply a new configuration in place (devices and deployments). Serving settings and
        listen addresses need a restart. Returns the ids of sessions that were closed."""
        deployments, groups, backends = self._build(config, set(self.core.groups))
        shadows, runners = self._build_shadows(config, self._shadows)
        self.devices = DeviceRegistry(config.devices, site_id=config.site_id)
        closed = self.core.replace_deployments(deployments, groups)
        self.core.replace_shadows(shadows)
        for revision_id, runner in self._shadows.items():
            if runners.get(revision_id) is not runner:
                self._tasks.append(asyncio.create_task(self._retire_shadow(runner)))
        for revision_id, runner in runners.items():
            if self._shadows.get(revision_id) is not runner and self._server is not None:
                self._launch_shadow(runner)
        self._shadows = runners
        closed += self.core.close_robots_without_credentials(self.devices.robots())
        for group in backends.values():
            self._groups[group.key] = group
            if self._server is not None:
                self._launch(group)
        for session_id in closed:
            queue = self._streams.pop(session_id, None)
            if queue is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(closed_message(session_id))
        for key in self.core.unused_groups():
            self.core.remove_group(key)
            retired = self._groups.pop(key, None)
            if retired is not None:
                self._tasks.append(asyncio.create_task(self._retire(retired)))
        self.config = config
        log.info(
            "configuration reloaded: %d deployments, %d sessions closed",
            len(deployments),
            len(closed),
        )
        return closed

    def _launch(self, group: _Group) -> None:
        async def run() -> None:
            await group.backend.start()
            await self._dispatch(group)

        group.task = asyncio.create_task(run(), name=f"dispatch-{group.key[:16]}")

    def _launch_shadow(self, shadow: _Shadow) -> None:
        async def run() -> None:
            await shadow.backend.start()
            await self._shadow_dispatch(shadow)

        shadow.task = asyncio.create_task(run(), name=f"shadow-{shadow.revision_id[:16]}")

    async def _retire_shadow(self, shadow: _Shadow) -> None:
        if shadow.task is not None:
            shadow.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await shadow.task
        await shadow.backend.stop()
        log.info("shadow of %s retired", shadow.revision_id)

    async def _shadow_dispatch(self, shadow: _Shadow) -> None:
        """Run the candidate on the observations its tracker takes, one at a time."""
        batch_ids = itertools.count(1)
        while True:
            await shadow.wake.wait()
            shadow.wake.clear()
            request = self.core.next_shadow(shadow.revision_id)
            if request is None:
                continue
            output: NDArray[np.float32] | PolicyError
            try:
                outputs = await shadow.backend.infer(
                    next(batch_ids), [request.inputs], timeout_s=SHADOW_TIMEOUT_S
                )
                output = outputs[0] if len(outputs) == 1 else PolicyError("not one output")
            except PolicyError as exc:
                output = exc
            except Exception as exc:  # a backend bug must never stop the shadow's dispatcher
                log.exception("shadow backend %s failed", shadow.revision_id)
                output = PolicyError(f"{type(exc).__name__}: {exc}")
            self.core.shadow_done(shadow.revision_id, request, output)

    async def _retire(self, group: _Group) -> None:
        """Stop a group's dispatcher and worker, freeing its device memory."""
        if group.task is not None:
            group.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await group.task
        await group.backend.stop()
        log.info("batch group %s retired", group.key)

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        serving = self.config.serving
        if serving.backend == "workers" and serving.sandbox.kind == "container":
            await remove_stale(serving.sandbox.runtime)
        options = [
            ("grpc.max_receive_message_length", self.config.max_message_bytes),
            ("grpc.max_send_message_length", self.config.max_message_bytes),
        ]
        self._server = grpc.aio.server(
            options=options, maximum_concurrent_rpcs=self.config.max_concurrent_rpcs
        )
        dataplane_pb2_grpc.add_RobotGatewayServicer_to_server(RobotService(self), self._server)
        if self.config.tls is not None:
            self._tls = _RotatingTls(self.config.tls)
            self.bound_port = self._server.add_secure_port(
                self.config.listen, self._tls.credentials()
            )
        else:
            log.warning("gateway without TLS: robots authenticate with tokens (development only)")
            self.bound_port = self._server.add_insecure_port(self.config.listen)
        await self._server.start()
        for group in self._groups.values():
            self._launch(group)
        for shadow in self._shadows.values():
            self._launch_shadow(shadow)
        self._tasks.append(asyncio.create_task(self._sweep(), name="lease-sweeper"))
        if self.config.status_listen:
            host, _, port = self.config.status_listen.rpartition(":")
            self._status_server = EmbeddedServer(
                uvicorn.Config(self.status_app(), host=host, port=int(port), log_level="warning")
            )
            self._status_task = asyncio.create_task(self._status_server.serve(), name="status-http")
        log.info("gateway listening on %s (port %s)", self.config.listen, self.bound_port)

    async def stop(self) -> None:
        if self._status_server is not None and self._status_task is not None:
            # Asked to exit, it finishes its requests and its lifespan; cancelled, it cannot.
            self._status_server.should_exit = True
            await asyncio.wait({self._status_task}, timeout=5)
            self._status_task.cancel()
        tasks = [
            *self._tasks,
            *(g.task for g in self._groups.values() if g.task is not None),
            *(s.task for s in self._shadows.values() if s.task is not None),
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            await self._server.stop(grace=0.5)
        await asyncio.gather(
            *(g.backend.stop() for g in self._groups.values()),
            *(s.backend.stop() for s in self._shadows.values()),
        )
        if self._owned_worker_dir is not None:
            shutil.rmtree(self._owned_worker_dir, ignore_errors=True)
        if self._owned_store is not None:
            self._owned_store.close()
            self._owned_store = None

    async def serve_forever(self, until: asyncio.Event | None = None) -> None:
        """Serve until ``until`` is set (the entry point sets it on SIGTERM), then stop everything.

        Stopping is asked for, never done by cancelling this task: stop() waits with timeouts,
        and a task being cancelled has every timeout it waits on cancelled too.
        """
        await self.start()
        assert self._server is not None
        stopped = asyncio.ensure_future(self._server.wait_for_termination())
        asked = asyncio.ensure_future((until or asyncio.Event()).wait())
        try:
            await asyncio.wait({stopped, asked}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            asked.cancel()
            await self.stop()
            stopped.cancel()

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
        for revision_id, shadow in self._shadows.items():
            tracker = self.core.shadows.get(revision_id)
            if tracker is not None and tracker.pending is not None:
                shadow.wake.set()

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
        worker_timeout_us = int(self.config.serving.worker_timeout_ms * 1000)
        while True:
            await group.wake.wait()
            group.wake.clear()
            while True:
                batch, dropped = self.core.next_batch(group.key, monotonic_us())
                self.route(dropped)
                if batch is None:
                    hold = self.core.hold_until(group.key)
                    if hold is None:
                        break
                    # Holding the batch open: ask again at the deadline or on a new arrival.
                    with contextlib.suppress(TimeoutError):
                        delay = max(hold - monotonic_us(), 0) / 1e6
                        await asyncio.wait_for(group.wake.wait(), timeout=delay)
                    group.wake.clear()
                    continue
                outputs = await self._run(group, batch, worker_timeout_us)
                done_us = monotonic_us()
                self.metrics.batch_done(
                    group.key,
                    (done_us - batch.dispatched_at_us) / 1e6,
                    failed=isinstance(outputs, PolicyError),
                )
                self.route(self.core.complete(group.key, batch, outputs, done_us))

    @staticmethod
    async def _run(
        group: _Group, batch: Batch, worker_timeout_us: int
    ) -> list[NDArray[np.float32]] | PolicyError:
        # A result after every request's deadline is useless; a worker that takes longer than
        # that and the configured minimum is treated as hung.
        latest = max(r.deadline_us for r in batch.requests)
        timeout_us = max(worker_timeout_us, latest - monotonic_us())
        try:
            return await group.backend.infer(
                batch.batch_id, [r.inputs for r in batch.requests], timeout_s=timeout_us / 1e6
            )
        except PolicyError as exc:
            return exc
        except Exception as exc:  # a backend bug must never stop the group's dispatcher
            log.exception("backend %s failed", group.key)
            return PolicyError(f"{type(exc).__name__}: {exc}")

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            self.core.sweep(monotonic_us())
            for group in self._groups.values():
                group.wake.set()  # lets the scheduler expire stale requests promptly

    def assignment(self, robot_id: str) -> dataplane_pb2.Assignment:
        """The revision this robot should run and its documents, read from the files the
        configuration names (the agent verified them against the signed manifest)."""
        revision_id = self.config.assignments.get(robot_id)
        if revision_id is None or revision_id not in self.core.allowed_revisions(robot_id):
            return dataplane_pb2.Assignment()
        item = next(d for d in self.config.deployments if d.revision_id == revision_id)

        def as_json(path: Path) -> bytes:  # documents may be stored as YAML (a JSON superset)
            return json.dumps(yaml.safe_load(path.read_text(encoding="utf-8"))).encode()

        documents = {
            "model_artifact": as_json(item.artifact),
            "robot_profile": as_json(item.robot_profile),
            "execution_profile": as_json(item.execution_profile),
        }
        assignment = dataplane_pb2.Assignment(revision_id=revision_id, documents=documents)
        if item.capture is not None:
            capture = item.capture
            assignment.capture.CopyFrom(
                capture_pb2.CaptureSettings(
                    mode=(
                        capture_pb2.CAPTURE_MODE_ALL
                        if capture.mode == "all"
                        else capture_pb2.CAPTURE_MODE_FAILURES
                    ),
                    endpoint=capture.endpoint,
                    image_stride=capture.image_stride,
                    cameras=capture.cameras,
                    max_episode_seconds=capture.max_episode_seconds,
                    spool_max_bytes=capture.spool_max_bytes,
                    upload_bytes_per_second=capture.upload_bytes_per_second,
                )
            )
        return assignment

    def backends(self) -> dict[str, Backend]:
        """Each batch group's backend, by group key."""
        return {key: group.backend for key, group in self._groups.items()}

    # --- status -------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        status = self.core.status()
        status["uptime_s"] = round(time.time() - self.started_at, 1)
        for key, group in self._groups.items():
            if key in status["groups"]:
                status["groups"][key]["profile_source"] = group.profile_source
                status["groups"][key]["worker"] = group.backend.status()
        for revision_id, shadow in self._shadows.items():
            if revision_id in status["shadows"]:
                status["shadows"][revision_id]["worker"] = shadow.backend.status()
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

        @app.get("/metrics")
        async def metrics() -> Response:
            return Response(self.metrics.exposition(), media_type=CONTENT_TYPE_LATEST)

        return app


class _RotatingTls:
    """Server credentials that pick up a renewed certificate (or CA) on the next handshake."""

    def __init__(self, tls: TlsConfig) -> None:
        self._tls = tls
        self._stamp = self._mtimes()

    def _mtimes(self) -> tuple[int, ...]:
        return tuple(
            p.stat().st_mtime_ns
            for p in (self._tls.certificate, self._tls.key, self._tls.client_ca)
        )

    def _configuration(self) -> grpc.ServerCertificateConfiguration:
        return grpc.ssl_server_certificate_configuration(
            [(self._tls.key.read_bytes(), self._tls.certificate.read_bytes())],
            root_certificates=self._tls.client_ca.read_bytes(),
        )

    def _fetch(self) -> grpc.ServerCertificateConfiguration | None:
        try:
            stamp = self._mtimes()
        except OSError:
            return None  # mid-rename: keep the current configuration
        if stamp == self._stamp:
            return None
        self._stamp = stamp
        log.info("gateway TLS certificate reloaded")
        return self._configuration()

    def credentials(self) -> grpc.ServerCredentials:
        # The fetcher returns None for "unchanged", as gRPC documents; the stubs omit it.
        fetch = cast(Callable[[], grpc.ServerCertificateConfiguration], self._fetch)
        return grpc.dynamic_ssl_server_credentials(
            self._configuration(), fetch, require_client_authentication=True
        )
