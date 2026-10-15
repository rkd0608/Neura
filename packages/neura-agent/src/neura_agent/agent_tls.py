"""The agent's TLS client certificate for its stream to the control plane (design §17.2).

The key is generated on the server and never leaves it; the control plane's agent CA signs a CSR
at enrollment and again before the certificate expires, over the authenticated stream. The
certificate is presented on every connection, alongside the short-lived token signed with the
agent's identity key.
"""

from __future__ import annotations

from pathlib import Path

from neura_agent.identity import atomic_write
from neura_contracts import pki


class AgentTls:
    def __init__(self, state_dir: Path) -> None:
        self.directory = state_dir / "tls"
        self.key_path = self.directory / "client.key"
        self.certificate_path = self.directory / "client.pem"
        self.ca_path = self.directory / "agent-ca.pem"
        self._pending: bytes | None = None  # key PEM awaiting its renewed certificate

    def request(self, name: str) -> bytes:
        """A CSR for a new key. The key is kept until its certificate arrives."""
        key = pki.new_key()
        self._pending = pki.key_pem(key)
        return pki.signing_request(key, name)

    def install(self, certificate_pem: bytes, ca_pem: bytes) -> None:
        """Store the certificate for the pending key (enrollment or renewal)."""
        if self._pending is None:
            raise ValueError("no certificate request is pending")
        key = pki.load_key(self._pending)
        if pki.load_certificate(certificate_pem).public_key() != key.public_key():
            raise ValueError("the certificate is not for the requested key")
        atomic_write(self.key_path, self._pending, 0o600)
        atomic_write(self.certificate_path, certificate_pem, 0o644)
        if ca_pem:
            atomic_write(self.ca_path, ca_pem, 0o644)
        self._pending = None

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def credentials(self) -> tuple[bytes, bytes] | None:
        """(key PEM, certificate PEM), or None before enrollment issued one."""
        if not (self.key_path.exists() and self.certificate_path.exists()):
            return None
        return self.key_path.read_bytes(), self.certificate_path.read_bytes()

    def needs_renewal(self) -> bool:
        credentials = self.credentials()
        return credentials is not None and pki.needs_renewal(credentials[1])
