"""The site's certificate authority (design §17.3): mutual TLS between robots and the gateway.

The agent creates the site CA when the server is enrolled and keeps its key on the server
(`<state>/pki/ca.key`, mode 0600). It issues:

- the gateway's server certificate, for the names robots use to reach it, renewed automatically
  once a third of its lifetime remains;
- each robot's client certificate, from a CSR the robot made with a key that never leaves it,
  after the site operator approves it (`neura-agent robot-certificate`).

The gateway accepts a client certificate only if this CA issued it, it is valid, its fingerprint
is still allowed (revocation removes it) and it names the robot and this site.
"""

from __future__ import annotations

import datetime
import socket
from dataclasses import dataclass
from pathlib import Path

from neura_agent.identity import atomic_write
from neura_contracts import pki

GATEWAY_DAYS = 90
ROBOT_DAYS = 365


@dataclass(frozen=True, slots=True)
class GatewayTls:
    certificate: Path
    key: Path
    client_ca: Path


def default_gateway_names() -> list[str]:
    return sorted({socket.gethostname(), "localhost", "127.0.0.1"})


class SitePki:
    def __init__(self, directory: Path, site_id: str) -> None:
        self.directory = directory
        self.site_id = site_id
        self._authority: pki.CertificateAuthority | None = None

    @property
    def authority(self) -> pki.CertificateAuthority:
        if self._authority is None:
            cert, key = self.directory / "ca.pem", self.directory / "ca.key"
            if cert.exists() and key.exists():
                self._authority = pki.CertificateAuthority.load(cert.read_bytes(), key.read_bytes())
            else:
                self._authority = pki.CertificateAuthority.create(f"Neura site CA {self.site_id}")
                atomic_write(key, self._authority.key_pem, 0o600)
                atomic_write(cert, self._authority.certificate_pem, 0o644)
        return self._authority

    @property
    def ca_pem(self) -> bytes:
        return self.authority.certificate_pem

    def gateway_tls(self, directory: Path, names: list[str]) -> GatewayTls:
        """The gateway's certificate, key and client CA under ``directory``, issued or renewed
        when missing, expiring or for other names."""
        paths = GatewayTls(directory / "server.pem", directory / "server.key", directory / "ca.pem")
        wanted = sorted(set(names))
        current = paths.certificate.read_bytes() if paths.certificate.exists() else None
        if (
            current is None
            or not paths.key.exists()
            or pki.needs_renewal(current)
            or not self.authority.issued(current)
            or _names(current) != wanted
        ):
            key_pem, certificate = self.authority.issue(
                identity_uri=pki.gateway_uri(self.site_id),
                days=GATEWAY_DAYS,
                server_names=wanted,
                client=False,
            )
            # Key first: the gateway reloads when the certificate changes.
            atomic_write(paths.key, key_pem, 0o600)
            atomic_write(paths.certificate, certificate, 0o644)
        if not paths.client_ca.exists() or paths.client_ca.read_bytes() != self.ca_pem:
            atomic_write(paths.client_ca, self.ca_pem, 0o644)
        return paths

    def issue_robot(self, robot: str, request_pem: bytes) -> bytes:
        return self.authority.sign_request(
            request_pem, identity_uri=pki.robot_uri(self.site_id, robot), days=ROBOT_DAYS
        )

    def issue_robot_bundle(self, robot: str) -> tuple[bytes, bytes]:
        """Key and certificate made here, for development robots only: a real robot makes its
        own key and sends a CSR."""
        return self.authority.issue(
            identity_uri=pki.robot_uri(self.site_id, robot), days=ROBOT_DAYS
        )


def _names(certificate_pem: bytes) -> list[str]:
    from cryptography import x509

    cert = pki.load_certificate(certificate_pem)
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns = list(names.get_values_for_type(x509.DNSName))
    ips = [str(ip) for ip in names.get_values_for_type(x509.IPAddress)]
    return sorted({*dns, *ips})


def record(certificate_pem: bytes) -> dict[str, str]:
    """What the agent's state keeps about an issued robot certificate."""
    return {
        "sha256": pki.fingerprint(certificate_pem),
        "not_after": pki.not_after(certificate_pem).isoformat(),
    }


def unexpired(records: list[dict[str, str]], now: datetime.datetime | None = None) -> list[str]:
    now = now or datetime.datetime.now(datetime.UTC)
    return [r["sha256"] for r in records if datetime.datetime.fromisoformat(r["not_after"]) > now]
