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
from collections.abc import AsyncIterator, Callable
from typing import Any

import grpc

from neura.v1 import dataplane_pb2, dataplane_pb2_grpc
from neura_runtime.session import Outbound, SessionClient

log = logging.getLogger("neura.robot")
SEND_QUEUE = 8


def monotonic_us() -> int:
    return time.monotonic_ns() // 1000


class GatewayConnection:
    def __init__(
        self,
        client: SessionClient,
        target: str,
        token: str,
        *,
        rpc_timeout_s: float = 0.5,
        max_message_bytes: int = 32 * 1024 * 1024,
        on_tick: Callable[[int], None] | None = None,
        clock: Callable[[], int] = monotonic_us,
    ) -> None:
        self.client = client
        self.target = target
        self._metadata = (("authorization", f"Bearer {token}"),)
        self._rpc_timeout = rpc_timeout_s
        self._max_message_bytes = max_message_bytes
        self._on_tick = on_tick
        self._clock = clock
        self._send: asyncio.Queue[dataplane_pb2.RobotMessage] = asyncio.Queue(maxsize=SEND_QUEUE)
        self._calls: set[asyncio.Task[None]] = set()
        self.ticks = 0
        self.late_ticks = 0

    async def run(self, stop: asyncio.Event) -> None:
        options = [
            ("grpc.max_receive_message_length", self._max_message_bytes),
            ("grpc.max_send_message_length", self._max_message_bytes),
        ]
        async with grpc.aio.insecure_channel(self.target, options=options) as channel:
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
            try:
                call = stub.Stream(self._outgoing(), metadata=self._metadata)
                async for message in call:
                    backoff = 0.1
                    self.client.on_gateway_message(message, self._clock())
            except grpc.aio.AioRpcError as exc:
                log.debug("stream ended: %s", exc.code())
            self.client.on_stream_closed(self._clock())
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 2.0)

    async def _outgoing(self) -> AsyncIterator[dataplane_pb2.RobotMessage]:
        while True:
            yield await self._send.get()
