"""Publisher signatures on model artifacts: only a trusted key's valid signature is accepted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from neura_contracts import ReasonCode, cli, signing

DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32


def publisher() -> tuple[Ed25519PrivateKey, bytes]:
    key = signing.load_private_key(signing.generate_key())
    return key, signing.raw_public_key(key)


def test_a_trusted_publishers_signature_is_accepted() -> None:
    key, public = publisher()
    signature = signing.sign(DIGEST, key)
    assert signature["key_id"] == signing.key_id(public)
    assert signing.verify(DIGEST, signature)
    assert signing.trusted_signer(DIGEST, [signature], [public]) == signature["key_id"]


def test_untrusted_or_missing_signatures_are_refused() -> None:
    key, _ = publisher()
    _, trusted = publisher()
    with pytest.raises(signing.SignatureError) as untrusted:
        signing.trusted_signer(DIGEST, [signing.sign(DIGEST, key)], [trusted])
    assert untrusted.value.code is ReasonCode.AGENT_ARTIFACT_UNTRUSTED
    with pytest.raises(signing.SignatureError) as unsigned:
        signing.trusted_signer(DIGEST, [], [trusted])
    assert unsigned.value.code is ReasonCode.AGENT_ARTIFACT_UNTRUSTED
    with pytest.raises(signing.SignatureError, match="none configured"):
        signing.trusted_signer(DIGEST, [signing.sign(DIGEST, key)], [])


def test_a_trusted_keys_signature_for_another_artifact_is_invalid() -> None:
    key, public = publisher()
    moved = {**signing.sign(OTHER, key)}
    assert not signing.verify(DIGEST, moved)
    with pytest.raises(signing.SignatureError) as invalid:
        signing.trusted_signer(DIGEST, [moved], [public])
    assert invalid.value.code is ReasonCode.AGENT_ARTIFACT_SIGNATURE_INVALID


def test_malformed_signatures_are_rejected_by_the_api_and_ignored_by_sites() -> None:
    key, public = publisher()
    good = signing.sign(DIGEST, key)
    other_key_id = {**good, "key_id": "ed25519:0000000000000000"}
    for bad in (
        {"key_id": good["key_id"]},
        other_key_id,
        {**good, "signature": "!!"},
        {**good, "public_key": "00"},
    ):
        with pytest.raises(signing.SignatureError):
            signing.validate(DIGEST, [bad])
    # A site skips what it cannot parse and still finds the good signature.
    assert signing.trusted_signer(DIGEST, [other_key_id, good], [public]) == good["key_id"]


def test_the_api_keeps_one_signature_per_key_sorted() -> None:
    first, _ = publisher()
    second, _ = publisher()
    signatures = [signing.sign(DIGEST, k) for k in (first, second, first)]
    kept = signing.validate(DIGEST, signatures)
    assert [s["key_id"] for s in kept] == sorted({s["key_id"] for s in signatures})


def test_public_keys_parse_from_hex_pem_and_trust_files() -> None:
    key, public = publisher()
    pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    assert signing.parse_public_key(public.hex()) == public
    assert signing.parse_public_key(pem) == public
    _, other = publisher()
    trust = f"# release key\n{other.hex()}  # ci\n\n{pem}"
    assert signing.parse_public_keys(trust) == [public, other]
    with pytest.raises(signing.SignatureError):
        signing.parse_public_key("not-a-key")


def test_the_cli_signs_and_verifies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    artifact = Path(__file__).resolve().parents[3] / "examples/contracts/act-so101.artifact.yaml"
    key_path = tmp_path / "publisher.pem"
    assert cli.main(["publisher-key", "--out", str(key_path)]) == 0
    assert key_path.stat().st_mode & 0o777 == 0o600
    public = capsys.readouterr().out.split("public_key")[1].split()[0]
    assert cli.main(["publisher-key", "--out", str(key_path)]) == 2  # never overwrites
    capsys.readouterr()
    assert cli.main(["sign", str(artifact), "--key", str(key_path)]) == 0
    signature = tmp_path / "signature.json"
    signature.write_text(capsys.readouterr().out)
    assert json.loads(signature.read_text())["public_key"] == public
    verify = ["verify", str(artifact), "--signature", str(signature)]
    assert cli.main([*verify, "--trusted-key", public]) == 0
    assert cli.main([*verify, "--trusted-key", "00" * 32]) == 1
