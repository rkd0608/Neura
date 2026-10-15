"""The certificate authorities behind Neura's mutual TLS (design §17)."""

from __future__ import annotations

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from neura_contracts import pki


def test_a_ca_signs_a_request_with_the_identity_it_chose() -> None:
    ca = pki.CertificateAuthority.create("site CA")
    key = pki.new_key()
    # The request asks for a name; the CA ignores it and sets the identity it authenticated.
    request = pki.signing_request(key, "neura:robot:other-site/robot-9")
    cert = ca.sign_request(request, identity_uri=pki.robot_uri("site_1", "robot-0"), days=30)
    assert pki.identity(cert) == "neura:robot:site_1/robot-0"
    assert ca.issued(cert)
    assert not pki.CertificateAuthority.create("other").issued(cert)
    parsed = pki.load_certificate(cert)
    assert parsed.public_key() == key.public_key()
    usage = parsed.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH in usage
    assert not parsed.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_server_certificates_carry_their_names() -> None:
    ca = pki.CertificateAuthority.create("site CA")
    _, cert = ca.issue(
        identity_uri=pki.gateway_uri("site_1"),
        days=90,
        server_names=["gateway.local", "10.0.0.5"],
        client=False,
    )
    names = (
        pki.load_certificate(cert)
        .extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value
    )
    assert names.get_values_for_type(x509.DNSName) == ["gateway.local"]
    assert [str(i) for i in names.get_values_for_type(x509.IPAddress)] == ["10.0.0.5"]


def test_requests_must_be_valid_p256() -> None:
    ca = pki.CertificateAuthority.create("site CA")
    with pytest.raises(pki.CertificateError):
        ca.sign_request(b"not a csr", identity_uri="neura:robot:s/r", days=1)
    other_curve = ec.generate_private_key(ec.SECP384R1())
    request = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(other_curve, hashes.SHA256())
    )
    with pytest.raises(pki.CertificateError, match="P-256"):
        ca.sign_request(request.public_bytes(serialization.Encoding.PEM), identity_uri="x", days=1)
    with pytest.raises(pki.CertificateError):
        pki.load_key(
            ed25519.Ed25519PrivateKey.generate().private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


def test_a_ca_round_trips_and_detects_a_mismatched_key() -> None:
    ca = pki.CertificateAuthority.create("site CA")
    loaded = pki.CertificateAuthority.load(ca.certificate_pem, ca.key_pem)
    assert loaded.certificate == ca.certificate
    with pytest.raises(pki.CertificateError):
        pki.CertificateAuthority.load(ca.certificate_pem, pki.key_pem(pki.new_key()))


def test_renewal_starts_with_a_third_of_the_lifetime_left() -> None:
    ca = pki.CertificateAuthority.create("site CA")
    _, cert = ca.issue(identity_uri="neura:server:srv_1", days=30)
    issued = pki.load_certificate(cert).not_valid_before_utc
    assert not pki.needs_renewal(cert, now=issued + datetime.timedelta(days=15))
    assert pki.needs_renewal(cert, now=issued + datetime.timedelta(days=25))
    # Fingerprints are stable across PEM and DER.
    der = pki.load_certificate(cert).public_bytes(serialization.Encoding.DER)
    assert pki.fingerprint(cert) == pki.fingerprint(der)
