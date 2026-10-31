"""The observation stream's send queue across stream attempts."""

from __future__ import annotations

import asyncio

import pytest

from neura.v1 import dataplane_pb2
from neura_runtime.grpc_client import SEND_QUEUE, GatewayConnection, _Outbox


def _connection() -> GatewayConnection:
    # Only the send path is exercised: no channel is opened.
    return GatewayConnection(client=None, target="127.0.0.1:1", credentials="token")  # type: ignore[arg-type]


def _message(sequence: int) -> dataplane_pb2.RobotMessage:
    message = dataplane_pb2.RobotMessage()
    message.observation.envelope.sequence = sequence
    return message


async def _take(iterator: object, timeout: float = 0.5) -> dataplane_pb2.RobotMessage | None:
    try:
        return await asyncio.wait_for(iterator.__anext__(), timeout)  # type: ignore[attr-defined]
    except (asyncio.TimeoutError, StopAsyncIteration):
        return None


async def test_an_ended_attempt_takes_no_message_meant_for_the_next() -> None:
    connection = _connection()
    # Attempts that failed while the gateway was starting: gRPC left their request iterators
    # waiting for the next message.
    dead = []
    for _ in range(3):
        ended = asyncio.Event()
        iterator = connection._outgoing(ended)
        dead.append((ended, asyncio.ensure_future(_take(iterator, timeout=2.0))))
    await asyncio.sleep(0.05)
    for ended, _ in dead:
        ended.set()
    live = connection._outgoing(asyncio.Event())
    connection._send.put_nowait(_message(1))
    connection._send.put_nowait(_message(2))
    first, second = await _take(live), await _take(live)
    assert first is not None
    assert second is not None
    assert (first.observation.envelope.sequence, second.observation.envelope.sequence) == (1, 2)
    assert [await waiting for _, waiting in dead] == [None, None, None]


async def test_messages_keep_their_order_and_the_queue_is_bounded() -> None:
    outbox = _Outbox(SEND_QUEUE)
    for sequence in range(SEND_QUEUE):
        outbox.put_nowait(_message(sequence))
    with pytest.raises(asyncio.QueueFull):
        outbox.put_nowait(_message(99))
    taken = [outbox.pop() for _ in range(SEND_QUEUE)]
    assert [m.observation.envelope.sequence for m in taken if m is not None] == list(
        range(SEND_QUEUE)
    )
    assert outbox.pop() is None
    assert len(outbox) == 0
