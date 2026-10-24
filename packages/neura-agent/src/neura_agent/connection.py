"""The agent's outbound connection to the control plane (design §17.3).

The agent always dials out. It reconnects with backoff, re-sends its hello, facts and any operation
reports the control plane may have missed, and keeps the site running on committed state while the
connection is down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from importlib.metadata import PackageNotFoundError, version

import grpc
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from neura.v1 import common_pb2, site_pb2, site_pb2_grpc
from neura_agent.agent_tls import AgentTls
from neura_agent.core import AgentCore
from neura_agent.identity import connection_token
from neura_agent.settings import AgentSettings

log = logging.getLogger("neura.agent.connection")


def agent_version() -> str:
    try:
        return version("neura-agent")
    except PackageNotFoundError:  # pragma: no cover
        return "0.0.0+unknown"


MAX_RECEIVE_BYTES = 64 * 1024 * 1024


def open_channel(
    endpoint: str, settings: AgentSettings, client: tuple[bytes, bytes] | None = None
) -> grpc.aio.Channel:
    """The channel to the control plane: TLS unless explicitly disabled for development, with
    the agent's client certificate (``client`` = key and certificate PEM) once it has one."""
    options = [
        ("grpc.max_receive_message_length", MAX_RECEIVE_BYTES),
        # A ping every 20 s, answered within 10 s, or the stream fails and the agent reconnects:
        # a link that drops packets silently is noticed in about 30 s (the control plane pings
        # too, and shows the site offline).
        ("grpc.keepalive_time_ms", 20_000),
        ("grpc.keepalive_timeout_ms", 10_000),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),
    ]
    if settings.control_plane_insecure:
        return grpc.aio.insecure_channel(endpoint, options=options)
    roots = settings.control_plane_ca_file.read_bytes() if settings.control_plane_ca_file else None
    key, certificate = client if client is not None else (None, None)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=roots, private_key=key, certificate_chain=certificate
    )
    return grpc.aio.secure_channel(endpoint, credentials, options=options)


class ControlConnection:
    def __init__(
        self,
        core: AgentCore,
        key: Ed25519PrivateKey,
        facts: Callable[[], site_pb2.ServerFacts],
        *,
        max_backoff_s: float = 10.0,
    ) -> None:
        self.core = core
        self._key = key
        self._facts = facts
        self._max_backoff = max_backoff_s
        self.tls = AgentTls(core.settings.state_dir)
        self.connected = False
        self.last_error: str | None = None
        self.connections = 0

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 0.5
        while not stop.is_set():
            try:
                await self._session(stop)
                backoff = 0.5
            except grpc.aio.AioRpcError as exc:
                self.last_error = f"{exc.code().name}: {exc.details()}"
                log.warning("control plane connection failed: %s", self.last_error)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("control plane connection error")
            finally:
                self.connected = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _session(self, stop: asyncio.Event) -> None:
        identity = self.core.identity
        token = connection_token(self._key, identity.server_id)
        async with open_channel(
            identity.endpoint, self.core.settings, self.tls.credentials()
        ) as channel:
            stub = site_pb2_grpc.SiteControlStub(channel)
            call = stub.Connect(
                self._outgoing(stop), metadata=(("authorization", f"Bearer {token}"),)
            )
            async for message in call:
                if not self.connected:
                    self.connected = True
                    self.connections += 1
                    self.last_error = None
                    self._renew_if_due()
                kind = message.WhichOneof("body")
                if kind == "certificate":
                    try:
                        self.tls.install(message.certificate.certificate, message.certificate.ca)
                        log.info("installed a renewed TLS client certificate")
                    except ValueError as exc:
                        log.error("refused a client certificate: %s", exc)
                elif kind == "contact":
                    self.core.on_contact_proof(message.contact)
                elif kind == "desired_state":
                    ack = await self.core.on_desired_state(message.desired_state)
                    self.core.outbox.put_nowait(ack)
                elif kind == "command":
                    await self.core.on_command(message.command)
                if stop.is_set():
                    call.cancel()
                    return

    def _renew_if_due(self) -> None:
        """Ask for a new client certificate once a third of its lifetime remains."""
        if self.tls.needs_renewal() and not self.tls.pending:
            csr = self.tls.request(self.core.identity.server_id)
            self.core.outbox.put_nowait(
                site_pb2.AgentMessage(certificate_request=site_pb2.CertificateRequest(csr=csr))
            )

    async def _outgoing(self, stop: asyncio.Event) -> AsyncIterator[site_pb2.AgentMessage]:
        """Every message on the stream, through the outbound schema and into the ledger."""
        async for message in self._messages(stop):
            sent = self.core.outbound.prepare("agent", message)
            if sent is not None:
                yield sent

    async def _messages(self, stop: asyncio.Event) -> AsyncIterator[site_pb2.AgentMessage]:
        yield site_pb2.AgentMessage(
            hello=site_pb2.AgentHello(
                protocol_version=common_pb2.PROTOCOL_VERSION_1,
                site_id=self.core.identity.site_id,
                agent_version=agent_version(),
                committed_generation=self.core.state.manifest_generation,
            )
        )
        yield site_pb2.AgentMessage(facts=self._facts())
        yield self.core.inventory_message()
        for report in self.core.pending_reports():
            yield report
        while not stop.is_set():
            yield await self.core.outbox.get()
