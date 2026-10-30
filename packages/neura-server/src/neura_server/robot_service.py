"""The ``RobotGateway`` gRPC service robots call: who is calling, and their messages to and from
the gateway core (design §6, §11)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import grpc

from neura.v1 import common_pb2, dataplane_pb2, dataplane_pb2_grpc

if TYPE_CHECKING:
    from neura_server.grpc_server import GatewayServer

log = logging.getLogger("neura.gateway")
STREAM_QUEUE = 256


def monotonic_us() -> int:
    """The gateway's clock: every time the core is given comes from here."""
    return time.monotonic_ns() // 1000


def closed_message(session_id: str) -> dataplane_pb2.GatewayMessage:
    return dataplane_pb2.GatewayMessage(
        rejected=dataplane_pb2.RequestRejected(
            envelope=common_pb2.Envelope(session_id=session_id),
            reason=common_pb2.Reason(code="LEASE_EXPIRED", message="revision no longer active"),
        )
    )


class RobotService(dataplane_pb2_grpc.RobotGatewayServicer):  # type: ignore[misc]
    def __init__(self, server: GatewayServer) -> None:
        self._server = server

    async def _robot(self, context: grpc.aio.ServicerContext[Any, Any]) -> str:
        robot_id: str | None
        if self._server.config.tls is not None:
            # TLS verified the chain to the site CA and the validity period; the registry checks
            # the certificate is still allowed and names this robot.
            certificates = (context.auth_context() or {}).get("x509_pem_cert") or ()
            peer = next(iter(certificates), b"")
            robot_id = self._server.devices.authenticate_certificate(peer)
        else:
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

    async def GetAssignment(  # noqa: N802
        self,
        request: dataplane_pb2.GetAssignmentRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> dataplane_pb2.Assignment:
        robot_id = await self._robot(context)
        return self._server.assignment(robot_id)

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
        getter: asyncio.Task[dataplane_pb2.GatewayMessage] | None = None
        try:
            while True:
                getter = asyncio.create_task(outbox.get())
                done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    continue
                break
        finally:
            for task in (getter, reader):
                if task is not None and not task.done():
                    task.cancel()
            server.unbind_stream(outbox)
        # The robot ended its stream, or the gateway failed on one of its messages: that is a bug,
        # and the robot is told so instead of seeing a stream that ended normally.
        failure = reader.exception()
        if failure is not None:
            log.error("stream from %s failed", robot_id, exc_info=failure)
            await context.abort(grpc.StatusCode.INTERNAL, "the gateway failed on a message")
