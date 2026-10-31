"""A policy behind an openpi policy server (Physical Intelligence's ``serve_policy.py``): the
robot's ``PolicyClient`` for it, unchanged on the server's side (design §30.2).

openpi's wire, followed exactly: a websocket; on connecting, the server sends its metadata; then
each request is one msgpack message with numpy arrays packed as openpi packs them, answered by
one message whose ``actions`` is the chunk. A text frame is the server's error. Requests carry no
identifiers, so a request this side gives up on (superseded, fenced, too late) would have its
answer read as the next one's: the connection is dropped and opened afresh instead.

Each openpi policy names its inputs its own way (``observation/image``, ``observation/state``,
``prompt``; ALOHA's ``state`` and ``images`` holding channels-first frames), so ``OpenPIKeys`` maps
Neura's inputs to the server's names. Needs ``neura-runtime[openpi]``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_contracts import ReasonCode
from neura_runtime.policy import ModelInputs, PolicyError


@dataclass(frozen=True, slots=True)
class StateKey:
    key: str  # the server's name
    state: str  # the state input it is taken from
    start: int = 0
    stop: int | None = None  # a slice of the input's components; None to the end


@dataclass(frozen=True, slots=True)
class OpenPIKeys:
    state: list[StateKey]
    images: dict[str, str]  # the server's name -> camera role
    prompt: str | None = "prompt"  # the instruction's name, when the policy reads language
    images_under: str | None = None  # images nested in one dict under this name (ALOHA: images)
    channels_first: bool = False  # (3, height, width) frames instead of (height, width, 3)
    extra: dict[str, Any] = field(default_factory=dict)  # constant entries the server expects


def pack_array(value: Any) -> Any:
    if isinstance(value, np.ndarray | np.generic) and value.dtype.kind in ("V", "O", "c"):
        raise ValueError(f"openpi cannot carry {value.dtype} arrays")
    if isinstance(value, np.ndarray):
        return {
            b"__ndarray__": True,
            b"data": value.tobytes(),
            b"dtype": value.dtype.str,
            b"shape": value.shape,
        }
    if isinstance(value, np.generic):
        return {b"__npgeneric__": True, b"data": value.item(), b"dtype": value.dtype.str}
    return value


def unpack_array(value: dict[Any, Any]) -> Any:
    if b"__ndarray__" in value:
        return np.ndarray(
            buffer=value[b"data"], dtype=np.dtype(value[b"dtype"]), shape=value[b"shape"]
        )
    if b"__npgeneric__" in value:
        return np.dtype(value[b"dtype"]).type(value[b"data"])
    return value


def observation(inputs: ModelInputs, keys: OpenPIKeys) -> dict[str, Any]:
    out: dict[str, Any] = dict(keys.extra)
    for entry in keys.state:
        out[entry.key] = inputs.state[entry.state][entry.start : entry.stop]
    images = {}
    for name, role in keys.images.items():
        frame = inputs.images[role]
        images[name] = (
            np.ascontiguousarray(frame.transpose(2, 0, 1)) if keys.channels_first else frame
        )
    if keys.images_under is not None:
        out[keys.images_under] = images
    else:
        out.update(images)
    if keys.prompt is not None and inputs.instruction:
        out[keys.prompt] = inputs.instruction
    return out


class OpenPIPolicy:
    """A ``PolicyClient`` for an openpi policy server at ``uri`` (``ws://host:port``)."""

    def __init__(
        self, uri: str, keys: OpenPIKeys, *, api_key: str = "", connect_timeout_s: float = 5.0
    ) -> None:
        import msgpack

        self.uri = uri if uri.startswith("ws") else f"ws://{uri}"
        self.keys = keys
        self.metadata: dict[str, Any] = {}
        self._headers = {"Authorization": f"Api-Key {api_key}"} if api_key else None
        self._connect_timeout_s = connect_timeout_s
        self._packer = msgpack.Packer(default=pack_array)
        self._connection: Any = None
        self.reconnects = 0

    async def _connect(self) -> Any:
        import msgpack
        from websockets.asyncio.client import connect
        from websockets.exceptions import WebSocketException

        try:
            connection = await asyncio.wait_for(
                connect(
                    self.uri, compression=None, max_size=None, additional_headers=self._headers
                ),
                self._connect_timeout_s,
            )
            greeting = await asyncio.wait_for(connection.recv(), self._connect_timeout_s)
        except (OSError, asyncio.TimeoutError, EOFError, WebSocketException) as error:
            # EOFError: something accepted the connection and closed it before answering
            raise PolicyError(
                f"no openpi server answers at {self.uri}: {type(error).__name__}: {error}",
                reason=ReasonCode.WORKER_UNAVAILABLE,
            ) from error
        self.metadata = msgpack.unpackb(greeting, object_hook=unpack_array)
        return connection

    async def connect(self) -> dict[str, Any]:
        """Connect now, rather than at the first request; returns the server's metadata."""
        if self._connection is None:
            self._connection = await self._connect()
        return self.metadata

    async def infer(self, inputs: ModelInputs) -> NDArray[np.float32]:
        import msgpack
        from websockets.exceptions import WebSocketException

        await self.connect()
        try:
            await self._connection.send(self._packer.pack(observation(inputs, self.keys)))
            reply = await self._connection.recv()
        except asyncio.CancelledError:
            await self._drop()  # its answer must not be taken for the next request's
            raise
        except (OSError, asyncio.TimeoutError, WebSocketException) as error:
            await self._drop()
            raise PolicyError(
                f"the openpi server at {self.uri}: {type(error).__name__}: {error}",
                reason=ReasonCode.WORKER_UNAVAILABLE,
            ) from error
        if isinstance(reply, str):
            await self._drop()  # the server closes after an error
            raise PolicyError(f"the openpi policy failed: {reply.strip().splitlines()[-1]}")
        answer = msgpack.unpackb(reply, object_hook=unpack_array)
        actions = answer.get("actions") if isinstance(answer, dict) else None
        if not isinstance(actions, np.ndarray):
            raise PolicyError(
                "the openpi server's answer has no actions", reason=ReasonCode.WORKER_OUTPUT_INVALID
            )
        return np.asarray(actions, dtype=np.float32)

    async def _drop(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            self.reconnects += 1
            await connection.close()  # returns at once on a connection already closed

    async def close(self) -> None:
        await self._drop()
