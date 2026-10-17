"""The site's capture service (design §28.2): robots upload finished episodes here.

It runs in the site agent, never in the gateway: an upload must not compete with the action
path for the gateway's event loop. Robots authenticate as they do to the gateway (a client
certificate from the site CA, or a development token), may upload only their own episodes, and
only for a revision whose deployment captures episodes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import grpc

from neura.v1 import capture_pb2, capture_pb2_grpc
from neura_server.capture.store import EpisodeStore, StoreError
from neura_server.config import DeviceRegistry, TlsConfig
from neura_server.grpc_server import _RotatingTls

log = logging.getLogger("neura.capture")

# Parts are 1 MiB; leave room for the header's manifest.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

Allowed = Callable[[str, str], bool]  # (robot_id, revision_id) -> may it upload?
Stored = Callable[[dict[str, Any]], None]


class CaptureServicer(capture_pb2_grpc.CaptureServicer):  # type: ignore[misc]
    def __init__(
        self,
        store: EpisodeStore,
        registry: DeviceRegistry,
        *,
        tls: bool,
        allowed: Allowed,
        on_stored: Stored | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.tls = tls
        self.allowed = allowed
        self.on_stored = on_stored
        self.counters: dict[str, int] = {}

    def _count(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    async def _robot(self, context: grpc.aio.ServicerContext[Any, Any]) -> str:
        robot_id: str | None
        if self.tls:
            certificates = (context.auth_context() or {}).get("x509_pem_cert") or ()
            robot_id = self.registry.authenticate_certificate(next(iter(certificates), b""))
        else:
            header = str(dict(context.invocation_metadata() or ()).get("authorization", ""))
            token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
            robot_id = self.registry.authenticate(token) if token else None
        if robot_id is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "AUTH_DEVICE_UNKNOWN")
            raise AssertionError("unreachable")
        return robot_id

    async def UploadEpisode(  # noqa: N802 - gRPC method name
        self,
        request_iterator: AsyncIterator[capture_pb2.EpisodeUpload],
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> capture_pb2.EpisodeReceipt:
        robot_id = await self._robot(context)
        first = await anext(aiter(request_iterator), None)
        if first is None or not first.HasField("header"):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "CAPTURE_EPISODE_INVALID")
        header = first.header
        if await asyncio.to_thread(self.store.has, header.episode_id):
            self._count("duplicates")
            return capture_pb2.EpisodeReceipt(episode_id=header.episode_id, stored=False)
        try:
            upload = await asyncio.to_thread(self.store.begin, header.manifest, robot_id=robot_id)
        except StoreError as exc:
            self._count("refused")
            log.warning("refused an episode from %s: %s", robot_id, exc)
            await context.abort(_status(exc.code), f"{exc.code}: {exc}")
            raise AssertionError("unreachable") from exc
        manifest = upload.manifest
        if manifest["episode_id"] != header.episode_id:
            upload.abort()
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "CAPTURE_EPISODE_INVALID")
        if not self.allowed(robot_id, manifest["revision_id"]):
            upload.abort()
            self._count("not_enabled")
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                f"CAPTURE_NOT_ENABLED: {manifest['revision_id']} does not capture for {robot_id}",
            )
        try:
            async for part in request_iterator:
                if not part.HasField("data"):
                    raise StoreError("CAPTURE_EPISODE_INVALID", "a second header")
                await asyncio.to_thread(upload.write, part.data.file, part.data.data)
            stored = await asyncio.to_thread(upload.commit)
        except StoreError as exc:
            upload.abort()
            self._count("refused")
            log.warning("refused episode %s from %s: %s", header.episode_id, robot_id, exc)
            await context.abort(_status(exc.code), f"{exc.code}: {exc}")
            raise AssertionError("unreachable") from exc
        except BaseException:
            upload.abort()
            raise
        self._count("stored" if stored else "duplicates")
        if stored:
            deleted = await asyncio.to_thread(self.store.enforce_retention)
            if deleted:
                log.info("retention deleted %d episodes", len(deleted))
            if self.on_stored is not None:
                self.on_stored(await asyncio.to_thread(self.store.get, header.episode_id))
        return capture_pb2.EpisodeReceipt(episode_id=header.episode_id, stored=stored)


def _status(code: str) -> grpc.StatusCode:
    if code == "CAPTURE_ROBOT_MISMATCH":
        return grpc.StatusCode.PERMISSION_DENIED
    if code == "CAPTURE_EPISODE_TOO_LARGE":
        return grpc.StatusCode.RESOURCE_EXHAUSTED
    return grpc.StatusCode.INVALID_ARGUMENT


class CaptureServer:
    """The capture service on its own port. ``update`` takes new credentials and capture
    settings without a restart."""

    def __init__(
        self,
        store: EpisodeStore,
        listen: str,
        *,
        registry: DeviceRegistry,
        tls: TlsConfig | None,
        allowed: Allowed,
        on_stored: Stored | None = None,
    ) -> None:
        self.listen = listen
        self.servicer = CaptureServicer(
            store, registry, tls=tls is not None, allowed=allowed, on_stored=on_stored
        )
        self._tls = _RotatingTls(tls) if tls is not None else None
        self._server: grpc.aio.Server | None = None
        self.port: int | None = None

    def update(self, registry: DeviceRegistry, allowed: Allowed) -> None:
        self.servicer.registry = registry
        self.servicer.allowed = allowed

    async def start(self) -> None:
        server = grpc.aio.server(
            options=[
                ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
                ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
            ],
            maximum_concurrent_rpcs=16,
        )
        capture_pb2_grpc.add_CaptureServicer_to_server(self.servicer, server)
        if self._tls is not None:
            self.port = server.add_secure_port(self.listen, self._tls.credentials())
        else:
            self.port = server.add_insecure_port(self.listen)
        await server.start()
        self._server = server
        log.info("capture service listening on %s", self.listen)

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop(grace=2.0)
            self._server = None
