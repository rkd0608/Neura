"""asyncio/gRPC adapter that moves ``SessionClient`` messages to and from a site gateway.

The control loop ticks the runtime at its control period on the robot's monotonic clock. Network
calls never block it: unary calls run as separate tasks with deadlines, and the observation stream
has a bounded send queue. A production integration that needs tighter timing runs this loop in a
dedicated process and keeps the controller on its own real-time thread (design §9 component
choices); the runtime's sans-IO core is the same either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc

from neura.v1 import dataplane_pb2, dataplane_pb2_grpc
from neura_runtime.session import Outbound, SessionClient

log = logging.getLogger("neura.robot")
SEND_QUEUE = 8


def monotonic_us() -> int:
    return time.monotonic_ns() // 1000


@dataclass(frozen=True, slots=True)
class RobotCredentials:
    """How a robot proves its identity to the gateway: a client certificate from the site CA
    (mutual TLS, design §17.3), or a device token on a development gateway without TLS."""

    token: str | None = None
    ca_pem: bytes | None = None
    certificate_pem: bytes | None = None
    key_pem: bytes | None = None
    # The name to verify in the gateway's certificate when the target address is not in it.
    server_name: str | None = None

    def __post_init__(self) -> None:
        tls = (self.ca_pem, self.certificate_pem, self.key_pem)
        if self.token is None and any(v is None for v in tls):
            raise ValueError("credentials need a CA, certificate and key, or a token")
        if self.token is not None and any(v is not None for v in tls):
            raise ValueError("credentials are a certificate or a token, not both")

    @classmethod
    def from_directory(cls, directory: Path, robot: str) -> RobotCredentials:
        """``<robot>.key``, ``<robot>.pem`` and ``ca.pem`` in ``directory``."""
        return cls(
            ca_pem=(directory / "ca.pem").read_bytes(),
            certificate_pem=(directory / f"{robot}.pem").read_bytes(),
            key_pem=(directory / f"{robot}.key").read_bytes(),
        )

    @property
    def tls(self) -> bool:
        return self.token is None

    def channel(self, target: str, options: list[tuple[str, Any]]) -> grpc.aio.Channel:
        if not self.tls:
            return grpc.aio.insecure_channel(target, options=options)
        credentials = grpc.ssl_channel_credentials(
            root_certificates=self.ca_pem,
            private_key=self.key_pem,
            certificate_chain=self.certificate_pem,
        )
        if self.server_name:
            options = [*options, ("grpc.ssl_target_name_override", self.server_name)]
        return grpc.aio.secure_channel(target, credentials, options=options)

    def sync_channel(
        self, target: str, options: list[tuple[str, Any]] | None = None
    ) -> grpc.Channel:
        """A blocking channel, for work off the control loop (episode upload)."""
        options = list(options or [])
        if not self.tls:
            return grpc.insecure_channel(target, options=options)
        credentials = grpc.ssl_channel_credentials(
            root_certificates=self.ca_pem,
            private_key=self.key_pem,
            certificate_chain=self.certificate_pem,
        )
        if self.server_name:
            options = [*options, ("grpc.ssl_target_name_override", self.server_name)]
        return grpc.secure_channel(target, credentials, options=options)

    def metadata(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {self.token}"),) if self.token else ()


class GatewayConnection:
    def __init__(
        self,
        client: SessionClient,
        target: str,
        credentials: RobotCredentials | str,
        *,
        rpc_timeout_s: float = 0.5,
        max_message_bytes: int = 32 * 1024 * 1024,
        on_tick: Callable[[int], None] | None = None,
        clock: Callable[[], int] = monotonic_us,
    ) -> None:
        self.client = client
        self.target = target
        if isinstance(credentials, str):
            credentials = RobotCredentials(token=credentials)
        self.credentials = credentials
        self._metadata = credentials.metadata()
        self._rpc_timeout = rpc_timeout_s
        self._max_message_bytes = max_message_bytes
        self._on_tick = on_tick
        self._clock = clock
        self._send = _Outbox(SEND_QUEUE)
        self._calls: set[asyncio.Task[None]] = set()
        self.ticks = 0
        self.late_ticks = 0

    async def run(self, stop: asyncio.Event) -> None:
        options = [
            ("grpc.max_receive_message_length", self._max_message_bytes),
            ("grpc.max_send_message_length", self._max_message_bytes),
            # One connection per robot, even when several share a process (simulators, tests).
            ("grpc.use_local_subchannel_pool", 1),
        ]
        async with self.credentials.channel(self.target, options) as channel:
            stub = dataplane_pb2_grpc.RobotGatewayStub(channel)
            stream_task = asyncio.create_task(self._stream_forever(stub, stop))
            try:
                await self._control_loop(stub, stop)
            finally:
                stream_task.cancel()
                for task in list(self._calls):
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
                close = self.client.close()
                if close is not None:
                    with contextlib.suppress(grpc.aio.AioRpcError, asyncio.TimeoutError):
                        await stub.Control(
                            close, timeout=self._rpc_timeout, metadata=self._metadata
                        )

    async def _control_loop(
        self, stub: dataplane_pb2_grpc.RobotGatewayStub, stop: asyncio.Event
    ) -> None:
        period_s = self.client.runtime.binding.control_period_us / 1e6
        next_tick = time.monotonic()
        while not stop.is_set():
            now = self._clock()
            for item in self.client.tick(now):
                self._dispatch(stub, item)
            if self._on_tick is not None:
                self._on_tick(now)
                for item in self.client.drain():
                    self._dispatch(stub, item)
            self.ticks += 1
            next_tick += period_s
            delay = next_tick - time.monotonic()
            if delay < 0:
                self.late_ticks += 1
                next_tick = time.monotonic()  # do not try to catch up with a burst of ticks
                delay = 0
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    def _dispatch(self, stub: dataplane_pb2_grpc.RobotGatewayStub, item: Outbound) -> None:
        if item.channel == "stream":
            assert isinstance(item.message, dataplane_pb2.RobotMessage)
            try:
                self._send.put_nowait(item.message)
            except asyncio.QueueFull:
                log.warning("observation stream is backed up; dropping an observation")
            return
        task = asyncio.create_task(self._unary(stub, item))
        self._calls.add(task)
        task.add_done_callback(self._calls.discard)

    async def _unary(self, stub: dataplane_pb2_grpc.RobotGatewayStub, item: Outbound) -> None:
        kwargs: dict[str, Any] = {"timeout": self._rpc_timeout, "metadata": self._metadata}
        try:
            if item.channel == "open":
                assert isinstance(item.message, dataplane_pb2.OpenSessionRequest)
                response = await stub.OpenSession(item.message, **kwargs)
                self.client.on_open_response(response, self._clock())
            elif item.channel == "heartbeat":
                assert isinstance(item.message, dataplane_pb2.HeartbeatRequest)
                heartbeat = await stub.Heartbeat(item.message, **kwargs)
                self.client.on_heartbeat_response(heartbeat, self._clock())
            elif item.channel == "control":
                assert isinstance(item.message, dataplane_pb2.ControlRequest)
                control = await stub.Control(item.message, **kwargs)
                self.client.on_control_response(control, self._clock())
        except (TimeoutError, grpc.aio.AioRpcError) as exc:
            log.debug("%s call failed: %s", item.channel, exc)
            if item.channel == "open":
                self.client.on_open_failed(self._clock())
            elif item.channel == "heartbeat":
                self.client.on_heartbeat_failed(self._clock())

    async def _stream_forever(
        self, stub: dataplane_pb2_grpc.RobotGatewayStub, stop: asyncio.Event
    ) -> None:
        backoff = 0.1
        while not stop.is_set():
            ended = asyncio.Event()
            try:
                call = stub.Stream(self._outgoing(ended), metadata=self._metadata)
                async for message in call:
                    backoff = 0.1
                    self.client.on_gateway_message(message, self._clock())
            except grpc.aio.AioRpcError as exc:
                log.debug("stream ended: %s", exc.code())
            finally:
                ended.set()
            self.client.on_stream_closed(self._clock())
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 2.0)

    async def _outgoing(self, ended: asyncio.Event) -> AsyncIterator[dataplane_pb2.RobotMessage]:
        """The messages of one stream attempt. gRPC can leave the request iterator of a call
        that already failed waiting here: once its attempt has ended it takes nothing more, so
        no message meant for the next attempt goes to a dead call."""
        while not ended.is_set():
            message = self._send.pop()
            if message is not None:
                yield message  # taken and handed over without yielding to the loop in between
                continue
            arrived = asyncio.ensure_future(self._send.wait())
            gone = asyncio.ensure_future(ended.wait())
            try:
                await asyncio.wait({arrived, gone}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                arrived.cancel()
                gone.cancel()


class _Outbox:
    """The observation stream's bounded send queue (FIFO)."""

    def __init__(self, maxsize: int) -> None:
        self._items: deque[dataplane_pb2.RobotMessage] = deque()
        self._maxsize = maxsize
        self._ready = asyncio.Event()

    def put_nowait(self, message: dataplane_pb2.RobotMessage) -> None:
        if len(self._items) >= self._maxsize:
            raise asyncio.QueueFull
        self._items.append(message)
        self._ready.set()

    def pop(self) -> dataplane_pb2.RobotMessage | None:
        if not self._items:
            return None
        message = self._items.popleft()
        if not self._items:
            self._ready.clear()
        return message

    async def wait(self) -> None:
        await self._ready.wait()

    def __len__(self) -> int:
        return len(self._items)


async def fetch_assignment(
    target: str, credentials: RobotCredentials | str, *, timeout_s: float = 2.0
) -> dataplane_pb2.Assignment:
    """Ask the gateway which revision this robot should run (``GetAssignment``)."""
    if isinstance(credentials, str):
        credentials = RobotCredentials(token=credentials)
    async with credentials.channel(target, []) as channel:
        stub = dataplane_pb2_grpc.RobotGatewayStub(channel)
        assignment: dataplane_pb2.Assignment = await stub.GetAssignment(
            dataplane_pb2.GetAssignmentRequest(), timeout=timeout_s, metadata=credentials.metadata()
        )
        return assignment
