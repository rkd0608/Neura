"""Sealed weights from Neura's registry (design §18.2), as the artifact source ``neura``.

The agent fetches over the same authenticated, outbound channel as its stream: TLS with its
client certificate, and a short-lived token signed with its enrolled key. The control plane only
holds weights sealed with the customer's key; the artifact store opens them with the key
configured on this server and verifies the plaintext digest.
"""

from __future__ import annotations

import logging
from typing import BinaryIO

import grpc
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from neura.v1 import site_pb2, site_pb2_grpc
from neura_agent.agent_tls import AgentTls
from neura_agent.identity import Identity, connection_token
from neura_agent.settings import AgentSettings

log = logging.getLogger("neura.agent.registry")

SOURCE = "neura"


class NeuraRegistry:
    """Fetches one sealed file at a time; called from the artifact store's worker thread."""

    def __init__(
        self,
        settings: AgentSettings,
        identity: Identity,
        key: Ed25519PrivateKey,
        *,
        timeout_s: float = 3600.0,
    ) -> None:
        self._settings = settings
        self._identity = identity
        self._key = key
        self._tls = AgentTls(settings.state_dir)
        self._timeout_s = timeout_s

    def _channel(self) -> grpc.Channel:
        endpoint = self._identity.endpoint
        if self._settings.control_plane_insecure:
            return grpc.insecure_channel(endpoint)
        ca_file = self._settings.control_plane_ca_file
        key, certificate = self._tls.credentials() or (None, None)
        credentials = grpc.ssl_channel_credentials(
            root_certificates=ca_file.read_bytes() if ca_file else None,
            private_key=key,
            certificate_chain=certificate,
        )
        return grpc.secure_channel(endpoint, credentials)

    def __call__(self, digest: str, out: BinaryIO) -> bool:
        """Write the sealed file for ``digest`` to ``out``. False if the registry does not hold
        it or cannot be reached (the store then tries its other sources)."""
        token = connection_token(self._key, self._identity.server_id)
        with self._channel() as channel:
            stub = site_pb2_grpc.SiteControlStub(channel)
            try:
                for chunk in stub.FetchSealedWeights(
                    site_pb2.FetchSealedWeightsRequest(digest=digest),
                    metadata=(("authorization", f"Bearer {token}"),),
                    timeout=self._timeout_s,
                ):
                    out.write(chunk.data)
            except grpc.RpcError as exc:
                status = getattr(exc, "code", None)  # a failed call is also a grpc.Call
                if not callable(status) or status() is not grpc.StatusCode.NOT_FOUND:
                    log.warning("Neura's registry failed for %s: %s", digest, exc)
                return False
        return True
