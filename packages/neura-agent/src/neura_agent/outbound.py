"""Enforcing the outbound schema (design §18.2).

Before anything leaves a site, the agent passes it through :func:`filter_message`. Fields the
schema (``catalog/outbound.yaml``) does not list are never sent; fields the telemetry mode
excludes are withheld. Operation results (``operation.result_json``) are filtered key by key, and
telemetry metrics by name and label. What was withheld is reported back so the outbound ledger can
record it next to what was sent.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from google.protobuf.message import Message

from neura_agent.ledger import OutboundLedger
from neura_agent.settings import AgentSettings
from neura_contracts.outbound_schema import MESSAGES, METRICS, RESULTS, OutboundField

log = logging.getLogger("neura.agent.outbound")

Mode = Literal["standard", "minimal"]
MODES: tuple[Mode, ...] = ("standard", "minimal")
RESULT_FIELD = "operation.result_json"


@dataclass(frozen=True, slots=True)
class Filtered[T: Message]:
    message: T
    # Paths dropped because the mode excludes them.
    withheld: tuple[str, ...]
    # Paths the schema does not list at all: a component tried to send something unpublished.
    unknown: tuple[str, ...]
    # False when nothing of the message may be sent in this mode (it is not sent at all).
    sendable: bool


def _sends(entry: OutboundField, mode: Mode) -> bool:
    return mode == "standard" or entry.minimal


def _prefixes(paths: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for path in paths:
        parts = path.split(".")
        out.update(".".join(parts[:i]) for i in range(1, len(parts)))
    return out


_INTERIOR = {kind: _prefixes(fields) for kind, fields in MESSAGES.items()}
_RESULT_PATHS = [tuple(path.split(".")) for path in RESULTS]


def filter_message[M: Message](message: M, kind: str, mode: Mode) -> Filtered[M]:
    """A copy of ``message`` holding only what ``kind`` (agent, enroll, fetch_sealed_weights)
    may send in ``mode``."""
    if mode not in MODES:
        raise ValueError(f"unknown telemetry mode {mode!r}")
    fields = MESSAGES[kind]
    copy = type(message)()
    copy.CopyFrom(message)
    withheld: list[str] = []
    unknown: list[str] = []
    _filter(copy, "", fields, _INTERIOR[kind], mode, withheld, unknown)
    sendable = True
    if kind == "agent":
        body = copy.WhichOneof("body")
        original = message.WhichOneof("body")
        sendable = original is not None and body == original and _any_sent(original, mode)
    return Filtered(copy, tuple(withheld), tuple(unknown), sendable)


def _any_sent(body: str, mode: Mode) -> bool:
    return any(
        path.split(".")[0] == body and _sends(entry, mode)
        for path, entry in MESSAGES["agent"].items()
    )


def _filter(
    message: Message,
    prefix: str,
    fields: dict[str, OutboundField],
    interior: set[str],
    mode: Mode,
    withheld: list[str],
    unknown: list[str],
) -> None:
    for descriptor, value in list(message.ListFields()):
        path = f"{prefix}{descriptor.name}"
        nested = descriptor.message_type
        is_map = nested is not None and nested.GetOptions().map_entry
        if nested is not None and not is_map and path in interior:
            items = value if descriptor.is_repeated else [value]
            for item in items:
                _filter(item, f"{path}.", fields, interior, mode, withheld, unknown)
            continue
        entry = fields.get(path)
        if entry is None:
            unknown.append(path)
            message.ClearField(descriptor.name)
        elif not _sends(entry, mode):
            withheld.append(path)
            message.ClearField(descriptor.name)
        elif path == RESULT_FIELD:
            result, dropped, extra = filter_result(json.loads(value), mode)
            withheld.extend(f"{RESULT_FIELD}:{p}" for p in dropped)
            unknown.extend(f"{RESULT_FIELD}:{p}" for p in extra)
            setattr(message, descriptor.name, json.dumps(result, sort_keys=True).encode())
    if prefix == "telemetry.":
        _filter_metrics(message, unknown)


def _filter_metrics(batch: Any, unknown: list[str]) -> None:
    kept = []
    for point in batch.metrics:
        metric = METRICS.get(point.name)
        if metric is None:
            unknown.append(f"telemetry.metrics:{point.name}")
            continue
        for label in [k for k in point.labels if k not in metric.labels]:
            unknown.append(f"telemetry.metrics:{point.name}.{label}")
            del point.labels[label]
        kept.append(point)
    if len(kept) != len(batch.metrics):
        copies = [type(p)() for p in kept]
        for copy, point in zip(copies, kept, strict=True):
            copy.CopyFrom(point)
        del batch.metrics[:]
        batch.metrics.extend(copies)


def filter_result(value: Any, mode: Mode) -> tuple[Any, list[str], list[str]]:
    """An operation result with only the keys the schema lists. Returns (result, withheld,
    unknown)."""
    withheld: list[str] = []
    unknown: list[str] = []

    def walk(node: Any, at: tuple[str, ...]) -> Any:
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, item in node.items():
            here = [p for p in _RESULT_PATHS if p[: len(at)] == at and len(p) > len(at)]
            matches = [p for p in here if p[len(at)] in (key, "*")]
            label = ".".join((*at, str(key)))
            if not matches:
                unknown.append(label)
                continue
            exact = [p for p in matches if p[len(at)] == key] or matches
            step = exact[0][len(at)]
            leaf = next((p for p in exact if len(p) == len(at) + 1), None)
            if leaf is not None:
                if _sends(RESULTS[".".join(leaf)], mode):
                    out[key] = item
                else:
                    withheld.append(label)
                continue
            child = walk(item, (*at, step))
            if isinstance(child, dict) and not child and isinstance(item, dict) and item:
                continue  # everything below was withheld or unknown
            out[key] = child
        return out

    return walk(value, ()), withheld, unknown


CHANNELS = {"agent": "stream", "enroll": "enroll", "fetch_sealed_weights": "fetch_sealed_weights"}


class Outbound:
    """The one way out of the site: filter by the schema and mode, record in the ledger, and
    hand back what may be sent (None when nothing of the message may be)."""

    def __init__(self, ledger: OutboundLedger, mode: Mode) -> None:
        self.ledger = ledger
        self.mode = mode
        self.sent: dict[str, int] = {}
        self.sent_bytes: dict[str, int] = {}
        self.withheld_fields = 0
        self.unknown_fields = 0
        self.withheld_messages = 0

    def prepare[T: Message](self, kind: str, message: T) -> T | None:
        filtered = filter_message(message, kind, self.mode)
        self.withheld_fields += len(filtered.withheld)
        if filtered.unknown:
            self.unknown_fields += len(filtered.unknown)
            log.error("not sending fields outside the outbound schema: %s", filtered.unknown)
        if not filtered.sendable:
            self.withheld_messages += 1
            return None
        entry = self.ledger.record(
            CHANNELS[kind],
            filtered.message,
            withheld=filtered.withheld,
            unknown=filtered.unknown,
            redact=REDACTED.get(kind, ()),
        )
        self.sent[entry["kind"]] = self.sent.get(entry["kind"], 0) + 1
        self.sent_bytes[entry["kind"]] = self.sent_bytes.get(entry["kind"], 0) + entry["bytes"]
        return filtered.message


# Secrets that are sent but not kept in the ledger's copy (its digest still covers them).
REDACTED: dict[str, tuple[str, ...]] = {"enroll": ("enrollment_token",)}


def site_outbound(settings: AgentSettings) -> Outbound:
    ledger = OutboundLedger(
        settings.state_dir / "outbound", max_bytes=settings.outbound_ledger_max_mib << 20
    )
    return Outbound(ledger, settings.telemetry_mode)
