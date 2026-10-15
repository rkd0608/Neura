"""The outbound ledger (design §18.2): every message this server sent to Neura, exactly as sent.

Customers verify what leaves their site instead of trusting it. The agent records each message
after the outbound filter, just before it goes on the wire: when, on which channel, its kind, its
size and SHA-256 as serialized, its content as JSON, and which fields the telemetry mode withheld.

Entries are JSON lines in ``<state>/outbound/ledger-<n>.jsonl``, rotated by size; the oldest
files are dropped beyond the configured total. Each entry carries ``prev``, the previous entry's
``chain``, and ``chain``, the SHA-256 of its own content including ``prev``: an edited entry no
longer matches its chain, and a removed or reordered one breaks a link
(``neura-agent outbound --verify``).
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

from neura_contracts import canonical_json

GENESIS = "0" * 64


@dataclass(frozen=True, slots=True)
class Verification:
    ok: bool
    entries: int
    # The first entry whose chain does not match, if any.
    broken_at: int | None = None


def _content(message: Message, kind: str | None) -> dict[str, Any]:
    content: dict[str, Any] = MessageToDict(message, preserving_proto_field_name=True)
    # Operation results are JSON bytes on the wire; show them as JSON here.
    operation = content.get("operation")
    if kind == "operation" and isinstance(operation, dict) and "result_json" in operation:
        raw = base64.b64decode(operation.pop("result_json"))
        operation["result"] = json.loads(raw) if raw else None
    return content


class OutboundLedger:
    """Safe to share between processes (the running agent, and CLI commands such as ``enroll``
    that send on their own): every append takes an exclusive lock on the ledger directory and
    chains to the last entry on disk, not to what this process saw last."""

    def __init__(
        self, directory: Path, *, file_bytes: int = 16 << 20, max_bytes: int = 256 << 20
    ) -> None:
        self.directory = directory
        self.file_bytes = file_bytes
        self.max_files = max(2, max_bytes // file_bytes)
        self._lock = threading.Lock()

    def _files(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(self.directory.glob("ledger-*.jsonl"), key=lambda p: int(p.stem[7:]))

    def _last(self, files: list[Path]) -> tuple[int, str]:
        """The newest complete entry's sequence number and chain (a line cut off by a crash is
        skipped: the next entry chains to the one before it)."""
        for path in reversed(files):
            with path.open("rb") as f:
                size = f.seek(0, os.SEEK_END)
                window = 1 << 16
                while True:
                    start = max(0, size - window)
                    f.seek(start)
                    lines = f.read(size - start).splitlines()
                    complete = lines if start == 0 else lines[1:]
                    for line in reversed(complete):
                        try:
                            last = json.loads(line)
                        except ValueError:
                            continue
                        return int(last["seq"]), str(last["chain"])
                    if start == 0:
                        break
                    window *= 4
        return 0, GENESIS

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock, (self.directory / ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def record(
        self,
        channel: str,
        message: Message,
        *,
        withheld: tuple[str, ...] = (),
        unknown: tuple[str, ...] = (),
        redact: tuple[str, ...] = (),
        at: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        """Append the message as it is about to be sent. Returns the entry. Top-level fields in
        ``redact`` (secrets) are kept as their SHA-256 only; ``sha256`` still covers the wire."""
        wire = message.SerializeToString()
        kind = message.WhichOneof("body") if channel == "stream" else None
        content = _content(message, kind)
        for name in redact:
            if name in content:
                digest = hashlib.sha256(str(content[name]).encode()).hexdigest()
                content[name] = f"redacted sha256:{digest}"
        with self._exclusive():
            files = self._files()
            sequence, chain = self._last(files)
            entry: dict[str, Any] = {
                "seq": sequence + 1,
                "at": (at or datetime.datetime.now(datetime.UTC)).isoformat(),
                "channel": channel,
                "kind": kind or channel,
                "bytes": len(wire),
                "sha256": hashlib.sha256(wire).hexdigest(),
                "message": content,
            }
            if withheld:
                entry["withheld"] = sorted(set(withheld))
            if unknown:
                entry["unknown"] = sorted(set(unknown))
            entry["prev"] = chain
            entry["chain"] = _chain(entry)
            line = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
            self._append(files, line.encode())
        return entry

    def _append(self, files: list[Path], line: bytes) -> None:
        index = int(files[-1].stem[7:]) if files else 1
        path = self.directory / f"ledger-{index}.jsonl"
        if path.exists() and path.stat().st_size + len(line) > self.file_bytes:
            path = self.directory / f"ledger-{index + 1}.jsonl"
            for old in files[: -(self.max_files - 1)]:
                old.unlink(missing_ok=True)
        with path.open("ab") as f:
            f.write(line)
            f.flush()

    def entries(self, *, limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
        """The newest entries first, optionally of one kind."""
        out: list[dict[str, Any]] = []
        for entry in self._reversed():
            if kind is None or entry["kind"] == kind:
                out.append(entry)
                if len(out) >= limit:
                    break
        return out

    def _reversed(self) -> Iterator[dict[str, Any]]:
        for path in reversed(self._files()):
            for line in reversed(path.read_bytes().splitlines()):
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    def summary(self) -> dict[str, dict[str, int]]:
        """Messages and bytes sent per kind, over the entries still kept."""
        totals: dict[str, dict[str, int]] = {}
        for entry in self._reversed():
            kind = totals.setdefault(entry["kind"], {"messages": 0, "bytes": 0})
            kind["messages"] += 1
            kind["bytes"] += int(entry["bytes"])
        return dict(sorted(totals.items()))

    def verify(self) -> Verification:
        """Check every kept entry's own chain and its link to the entry before. The oldest kept
        entry's link points into files already rotated away, so only its content is checked."""
        previous: str | None = None
        count = 0
        for path in self._files():
            for line in path.read_bytes().splitlines():
                count += 1
                try:
                    entry = json.loads(line)
                except ValueError:
                    return Verification(False, count, count)
                linked = previous is None or entry.get("prev") == previous
                if not linked or _chain(entry) != entry.get("chain"):
                    return Verification(False, count, int(entry.get("seq", count)))
                previous = entry["chain"]
        return Verification(True, count)


def _chain(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "chain"}
    return hashlib.sha256(canonical_json(body)).hexdigest()
