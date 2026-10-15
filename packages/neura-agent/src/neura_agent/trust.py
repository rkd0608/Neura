"""Publisher keys this server trusts (design §17.3, §17.4).

A site runs a model artifact only if a publisher key trusted here signed its digest. Trust is a
boundary of the server, not of the cloud: keys come from ``NEURA_AGENT_TRUSTED_PUBLISHER_KEYS``
and from a trust file on the server (``NEURA_AGENT_TRUSTED_PUBLISHER_KEYS_FILE``, by default
``<state>/trusted-publishers``), which the site operator edits with ``neura-agent
trust-publisher`` and ``distrust-publisher``. The agent reads them on every prepare, so a change
needs no restart. One key per line (hex), or PEM blocks; ``#`` starts a comment.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from neura_agent.identity import atomic_write
from neura_agent.settings import AgentSettings
from neura_contracts import signing


def trust_file(settings: AgentSettings) -> Path:
    return settings.trusted_publisher_keys_file or settings.state_dir / "trusted-publishers"


def trusted_publishers(settings: AgentSettings) -> list[bytes]:
    """Raw public keys. Raises OSError if a configured trust file is missing, SignatureError if a
    key is malformed."""
    keys = [signing.parse_public_key(k) for k in settings.trusted_publisher_keys]
    path = trust_file(settings)
    if path.exists() or settings.trusted_publisher_keys_file is not None:
        keys += signing.parse_public_keys(path.read_text(encoding="utf-8"))
    return keys


def trust(settings: AgentSettings, key_text: str, note: str = "") -> tuple[str, bool]:
    """Add a key to the trust file. Returns (key_id, whether it was new)."""
    public = signing.parse_public_key(key_text)
    path = trust_file(settings)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if public in signing.parse_public_keys(existing):
        return signing.key_id(public), False
    stamp = datetime.datetime.now(datetime.UTC).date().isoformat()
    comment = f"  # {signing.key_id(public)} {stamp}" + (f" {note}" if note else "")
    if existing and not existing.endswith("\n"):
        existing += "\n"
    atomic_write(path, f"{existing}{public.hex()}{comment}\n".encode(), 0o644)
    return signing.key_id(public), True


def distrust(settings: AgentSettings, key_id: str) -> bool:
    """Remove a key (by key_id) from the trust file. Keys set in the environment stay."""
    path = trust_file(settings)
    if not path.exists():
        return False
    kept, removed = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        content = line.split("#", 1)[0].strip()
        if content and signing.key_id(signing.parse_public_key(content)) == key_id:
            removed = True
            continue
        kept.append(line)
    if removed:
        atomic_write(path, ("\n".join(kept) + "\n" if kept else "").encode(), 0o644)
    return removed
