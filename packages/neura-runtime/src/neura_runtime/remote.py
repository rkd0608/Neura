"""A policy on another machine: ``neura-robot serve`` runs it behind the policy worker protocol
(``neura.v1.PolicyWorker``) and ``RemotePolicy`` reaches it from the robot (design §30.2).

The protocol is the one a site gateway uses with its policy workers: one ``Infer`` call carries
decoded inputs and returns chunks, and ``Describe`` says what is served. A robot checks the
description against its contracts before it asks for anything. Over a network, a token is
required: the server refuses to listen beyond loopback without one, and every call must carry
it. Traffic is not encrypted: use it on a trusted network, or put TLS in front of it.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import itertools
import logging
from collections.abc import Callable
from typing import Any

import grpc
import numpy as np
from numpy.typing import NDArray

from neura.v1 import common_pb2, worker_pb2, worker_pb2_grpc
from neura_contracts import ReasonCode
from neura_runtime import tensors
from neura_runtime.binding import ExecutionBinding
from neura_runtime.policy import ModelInputs, PolicyError

log = logging.getLogger("neura.remote")

MAX_MESSAGE_BYTES = 64 * 1024 * 1024
_GRPC_OPTIONS = [
    ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
    ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
]


# --- the wire format, shared with the site's policy workers


def encode_inputs(inputs: ModelInputs) -> worker_pb2.WorkerInputs:
    message = worker_pb2.WorkerInputs(instruction=inputs.instruction)
    for name, vector in inputs.state.items():
        message.state[name].CopyFrom(tensors.encode(vector))
    for role, image in inputs.images.items():
        message.images[role].CopyFrom(tensors.encode(image))
    if inputs.frozen_prefix is not None:
        message.frozen_prefix.CopyFrom(tensors.encode(inputs.frozen_prefix))
    return message


def decode_inputs(message: worker_pb2.WorkerInputs) -> ModelInputs:
    state = {
        name: np.asarray(tensors.decode(t), dtype=np.float32) for name, t in message.state.items()
    }
    images = {
        role: np.asarray(tensors.decode(t), dtype=np.uint8) for role, t in message.images.items()
    }
    prefix = (
        np.asarray(tensors.decode(message.frozen_prefix), dtype=np.float32)
        if message.HasField("frozen_prefix")
        else None
    )
    return ModelInputs(
        state=state, images=images, instruction=message.instruction, frozen_prefix=prefix
    )


def outputs_from(response: worker_pb2.InferResponse) -> list[NDArray[np.float32]]:
    """The chunks of a successful response."""
    return [
        np.asarray(tensors.decode(t, require_finite=False), dtype=np.float32)
        for t in response.chunks.chunks
    ]


# --- the robot's side


class RemotePolicy:
    """A ``PolicyClient`` for a policy served with ``neura-robot serve``."""

    def __init__(self, target: str, *, token: str = "", timeout_s: float = 1.0) -> None:
        self.target = target
        self._metadata = (("authorization", f"Bearer {token}"),) if token else ()
        self._timeout_s = timeout_s
        self._channel = grpc.aio.insecure_channel(target, options=_GRPC_OPTIONS)
        self._stub = worker_pb2_grpc.PolicyWorkerStub(self._channel)
        self._batches = itertools.count(1)

    async def check(self, binding: ExecutionBinding) -> worker_pb2.WorkerDescription:
        """What the server serves; PolicyError if it cannot answer, or if its chunks would not
        fit this robot's contracts."""
        try:
            description: worker_pb2.WorkerDescription = await self._stub.Describe(
                worker_pb2.DescribeRequest(), timeout=self._timeout_s, metadata=self._metadata
            )
        except grpc.aio.AioRpcError as error:
            raise PolicyError(
                f"no policy server answers at {self.target}: {error.details()}",
                reason=ReasonCode.WORKER_UNAVAILABLE,
            ) from error
        if description.chunk_length and description.chunk_length != binding.chunk_length:
            raise PolicyError(
                f"the server's policy makes chunks of {description.chunk_length} actions; "
                f"the robot's contracts expect {binding.chunk_length}",
                reason=ReasonCode.WORKER_LOAD_FAILED,
            )
        return description

    async def infer(self, inputs: ModelInputs) -> NDArray[np.float32]:
        batch_id = next(self._batches)
        request = worker_pb2.InferRequest(batch_id=batch_id, inputs=[encode_inputs(inputs)])
        try:
            response: worker_pb2.InferResponse = await self._stub.Infer(
                request, timeout=self._timeout_s, metadata=self._metadata
            )
        except grpc.aio.AioRpcError as error:
            raise PolicyError(
                f"the policy server at {self.target}: {error.code().name} {error.details()}",
                reason=ReasonCode.WORKER_UNAVAILABLE,
            ) from error
        if response.WhichOneof("result") == "error":
            reason = response.error.reason
            raise PolicyError(reason.message, reason=ReasonCode(reason.code))
        outputs = outputs_from(response)
        if response.batch_id != batch_id or len(outputs) != 1:
            raise PolicyError(
                "the policy server answered another request",
                reason=ReasonCode.WORKER_OUTPUT_INVALID,
            )
        return outputs[0]

    async def close(self) -> None:
        await self._channel.close()


# --- the server's side


class _Servicer(worker_pb2_grpc.PolicyWorkerServicer):  # type: ignore[misc]
    def __init__(
        self,
        infer: Callable[[ModelInputs], NDArray[np.float32]],
        description: worker_pb2.WorkerDescription,
        token: str,
    ) -> None:
        self._infer = infer
        self._description = description
        self._expected = f"Bearer {token}" if token else ""
        self._lock = asyncio.Lock()

    async def _authorize(self, context: grpc.aio.ServicerContext[Any, Any]) -> None:
        if not self._expected:
            return
        given = dict(context.invocation_metadata() or ()).get("authorization", "")
        if not hmac.compare_digest(str(given), self._expected):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "a valid token is required")

    async def Describe(  # noqa: N802 - gRPC method name
        self, request: worker_pb2.DescribeRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> worker_pb2.WorkerDescription:
        await self._authorize(context)
        return self._description

    async def Infer(  # noqa: N802
        self, request: worker_pb2.InferRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> worker_pb2.InferResponse:
        await self._authorize(context)

        def error(code: ReasonCode, message: str) -> worker_pb2.InferResponse:
            reason = common_pb2.Reason(code=code.value, message=message[:1000])
            return worker_pb2.InferResponse(
                batch_id=request.batch_id, error=worker_pb2.WorkerError(reason=reason)
            )

        try:
            batch = [decode_inputs(m) for m in request.inputs]
        except tensors.TensorError as exc:
            return error(ReasonCode.PROTOCOL_MESSAGE_INVALID, str(exc))
        outputs = []
        async with self._lock:  # one forward pass at a time
            for inputs in batch:
                try:
                    outputs.append(await asyncio.to_thread(self._infer, inputs))
                except PolicyError as exc:
                    return error(exc.reason, str(exc))
        chunks = [tensors.encode(np.ascontiguousarray(o, dtype=np.float32)) for o in outputs]
        return worker_pb2.InferResponse(
            batch_id=request.batch_id, chunks=worker_pb2.Chunks(chunks=chunks)
        )


async def serve(
    infer: Callable[[ModelInputs], NDArray[np.float32]],
    listen: str,
    *,
    description: worker_pb2.WorkerDescription,
    token: str = "",
) -> tuple[grpc.aio.Server, int]:
    """Start serving ``infer`` on ``listen`` (host:port; port 0 picks one). Returns the server
    and the port it listens on."""
    host = listen.rsplit(":", 1)[0].strip("[]")
    if not token and not _loopback(host):
        raise ValueError(f"serving on {host} needs a token: anyone on the network could drive it")
    server = grpc.aio.server(options=_GRPC_OPTIONS)
    worker_pb2_grpc.add_PolicyWorkerServicer_to_server(_Servicer(infer, description, token), server)
    port = server.add_insecure_port(listen)
    await server.start()
    return server, port


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
