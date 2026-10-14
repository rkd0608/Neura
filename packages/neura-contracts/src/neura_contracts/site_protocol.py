"""Constants the control plane and the site agent must agree on byte for byte (design §17.3)."""

from __future__ import annotations

MANIFEST_API_VERSION = "neura.dev/site-manifest/v1alpha1"

# Domain separation for the manifest key: a manifest is canonical JSON (it starts with "{"), so a
# contact proof can never be mistaken for one, or the reverse.
CONTACT_PROOF_CONTEXT = b"neura.contact-proof.v1\x00"
CONTACT_NONCE_BYTES = 16


def contact_proof_payload(site_id: str, nonce: bytes) -> bytes:
    """The bytes the control plane signs to prove contact with a site (ContactProof)."""
    return CONTACT_PROOF_CONTEXT + site_id.encode("ascii") + b"\x00" + nonce
