"""Certificates for Neura's mutual TLS (design §17.2, §17.3, §18.2).

Two kinds of authority, both run by Neura's own components:

- the control plane's **agent CA** signs each site agent's client certificate, at enrollment and
  on renewal over the authenticated stream;
- each site agent's **site CA** signs its gateway's server certificate and every robot's client
  certificate. Robot traffic never leaves the site, so neither does its trust root.

Identities are URIs in the subject alternative name: ``neura:server:<server_id>``,
``neura:robot:<site_id>/<robot_name>`` and ``neura:gateway:<site_id>``. A CA sets the identity
itself from what it authenticated; nothing a CSR asks for is copied into a certificate except its
public key. Keys are ECDSA P-256, which every TLS stack gRPC runs on accepts. Private keys are
generated where they are used and never travel: robots and agents send CSRs.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SCHEME = "neura"
CLOCK_SKEW = datetime.timedelta(minutes=5)


class CertificateError(ValueError):
    """A certificate, key or signing request is malformed or not acceptable."""


def server_uri(server_id: str) -> str:
    return f"{SCHEME}:server:{server_id}"


def robot_uri(site_id: str, robot_name: str) -> str:
    return f"{SCHEME}:robot:{site_id}/{robot_name}"


def gateway_uri(site_id: str) -> str:
    return f"{SCHEME}:gateway:{site_id}"


# --- keys and requests ------------------------------------------------------------------------


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def load_key(pem: bytes) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise CertificateError("keys must be ECDSA P-256")
    return key


def signing_request(key: ec.EllipticCurvePrivateKey, name: str) -> bytes:
    """A CSR for ``key``. ``name`` is informational: the CA decides the identity."""
    request = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name[:64])]))
        .sign(key, hashes.SHA256())
    )
    return request.public_bytes(serialization.Encoding.PEM)


def _load_request(pem: bytes) -> x509.CertificateSigningRequest:
    try:
        request = x509.load_pem_x509_csr(pem)
    except ValueError as exc:
        raise CertificateError(f"not a PEM certificate signing request: {exc}") from exc
    if not request.is_signature_valid:
        raise CertificateError("the signing request's signature is invalid")
    key = request.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise CertificateError("keys must be ECDSA P-256")
    return request


# --- certificates -----------------------------------------------------------------------------


def load_certificate(pem: bytes) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(pem)
    except ValueError as exc:
        raise CertificateError(f"not a PEM certificate: {exc}") from exc


def fingerprint(certificate: bytes | x509.Certificate) -> str:
    """SHA-256 of the DER encoding, hex: how allowlists name a certificate. Accepts PEM or DER
    bytes (gRPC hands servers the peer's certificate as PEM)."""
    if isinstance(certificate, bytes):
        der = (
            load_certificate(certificate).public_bytes(serialization.Encoding.DER)
            if certificate.lstrip().startswith(b"-----BEGIN")
            else certificate
        )
    else:
        der = certificate.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def identity(certificate: bytes | x509.Certificate) -> str | None:
    """The neura: URI a certificate was issued for, or None."""
    cert = load_certificate(certificate) if isinstance(certificate, bytes) else certificate
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    uris = list(names.get_values_for_type(x509.UniformResourceIdentifier))
    neura = [u for u in uris if u.startswith(f"{SCHEME}:")]
    return neura[0] if len(neura) == 1 else None


def not_after(certificate: bytes | x509.Certificate) -> datetime.datetime:
    cert = load_certificate(certificate) if isinstance(certificate, bytes) else certificate
    return cert.not_valid_after_utc


def needs_renewal(certificate: bytes, *, now: datetime.datetime | None = None) -> bool:
    """True once less than a third of the certificate's lifetime remains."""
    cert = load_certificate(certificate)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    return cert.not_valid_after_utc - now < lifetime / 3


@dataclass(frozen=True, slots=True)
class CertificateAuthority:
    key: ec.EllipticCurvePrivateKey
    certificate: x509.Certificate

    @classmethod
    def create(cls, common_name: str, *, days: int = 3650) -> CertificateAuthority:
        key = new_key()
        now = datetime.datetime.now(datetime.timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64])])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - CLOCK_SKEW)
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .sign(key, hashes.SHA256())
        )
        return cls(key, certificate)

    @classmethod
    def load(cls, certificate_pem: bytes, key_pem_bytes: bytes) -> CertificateAuthority:
        authority = cls(load_key(key_pem_bytes), load_certificate(certificate_pem))
        if authority.certificate.public_key() != authority.key.public_key():
            raise CertificateError("the CA key does not match its certificate")
        return authority

    @property
    def certificate_pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)

    @property
    def key_pem(self) -> bytes:
        return key_pem(self.key)

    def _issue(
        self,
        public_key: ec.EllipticCurvePublicKey,
        *,
        identity_uri: str,
        days: int,
        client: bool,
        server_names: Sequence[str],
    ) -> bytes:
        now = datetime.datetime.now(datetime.timezone.utc)
        alternative: list[x509.GeneralName] = [x509.UniformResourceIdentifier(identity_uri)]
        for name in server_names:
            try:
                alternative.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                alternative.append(x509.DNSName(name))
        usages = [ExtendedKeyUsageOID.CLIENT_AUTH] if client else []
        if server_names:
            usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity_uri[:64])]))
            .issuer_name(self.certificate.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - CLOCK_SKEW)
            .not_valid_after(
                min(now + datetime.timedelta(days=days), self.certificate.not_valid_after_utc)
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(alternative), critical=False)
            .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()),
                critical=False,
            )
            .sign(self.key, hashes.SHA256())
        )
        return certificate.public_bytes(serialization.Encoding.PEM)

    def sign_request(
        self,
        request_pem: bytes,
        *,
        identity_uri: str,
        days: int,
        server_names: Sequence[str] = (),
        client: bool = True,
    ) -> bytes:
        """A certificate for the CSR's key with the identity the caller authenticated."""
        request = _load_request(request_pem)
        public_key = request.public_key()
        assert isinstance(public_key, ec.EllipticCurvePublicKey)  # checked by _load_request
        return self._issue(
            public_key,
            identity_uri=identity_uri,
            days=days,
            client=client,
            server_names=server_names,
        )

    def issue(
        self,
        *,
        identity_uri: str,
        days: int,
        server_names: Sequence[str] = (),
        client: bool = True,
    ) -> tuple[bytes, bytes]:
        """A new key and certificate, for a component whose key this machine holds anyway (the
        gateway's server certificate). Returns (key PEM, certificate PEM)."""
        key = new_key()
        certificate = self._issue(
            key.public_key(),
            identity_uri=identity_uri,
            days=days,
            client=client,
            server_names=server_names,
        )
        return key_pem(key), certificate

    def issued(self, certificate_pem: bytes) -> bool:
        """Whether this CA signed the certificate (signature check only; TLS checks the rest)."""
        certificate = load_certificate(certificate_pem)
        try:
            certificate.verify_directly_issued_by(self.certificate)
        except (ValueError, TypeError, InvalidSignature):
            return False
        return True
