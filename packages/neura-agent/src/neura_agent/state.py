"""The agent's committed local state, persisted atomically as JSON.

This is what lets the site keep operating without the cloud (design §8, §13): the last verified
manifest, prepared revisions, which revision each robot may run, pending approvals, and the record
of commands already executed so none runs twice.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from neura_agent.identity import atomic_write

MAX_EXECUTED = 2000


@dataclass(slots=True)
class Assignment:
    """Revisions a robot may run. ``next`` is set between approval and the robot's switch."""

    current: str | None = None
    next: str | None = None

    def allowed(self) -> list[str]:
        return [r for r in (self.current, self.next) if r is not None]

    def preferred(self) -> str | None:
        return self.next or self.current


@dataclass(slots=True)
class Activation:
    command_id: str
    operation_id: str
    kind: str  # request_activation | rollback
    revision_id: str
    robot_names: list[str]
    requested_at: float
    approved_by: str | None = None
    approved_at: float | None = None


@dataclass(slots=True)
class AgentState:
    manifest_payload: str | None = None  # canonical JSON, as signed
    manifest_signature: str | None = None  # hex
    manifest_generation: int = 0
    prepared: dict[str, str] = field(default_factory=dict)  # revision id -> revision digest
    # revision id -> where its batch group runs: device, group, adapter, checkpoint and measured
    # profile paths (workers backend only).
    placements: dict[str, dict[str, Any]] = field(default_factory=dict)
    assignments: dict[str, Assignment] = field(default_factory=dict)  # robot name -> assignment
    activations: dict[str, Activation] = field(default_factory=dict)  # command id -> activation
    executed: list[str] = field(default_factory=list)  # command ids, oldest first
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)  # command id -> last report
    robot_credentials: dict[str, str] = field(default_factory=dict)  # robot name -> token sha256
    # robot name -> client certificates the site CA issued it: {"sha256", "not_after"}.
    robot_certificates: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    last_contact_unix: float | None = None
    # The longest time since contact this site has seen, kept up with the monotonic clock and
    # persisted, so moving the wall clock back cannot extend offline authorization.
    offline_elapsed_s: float = 0.0

    def manifest(self) -> dict[str, Any] | None:
        if self.manifest_payload is None:
            return None
        data: dict[str, Any] = json.loads(self.manifest_payload)
        return data

    def mark_executed(self, command_id: str) -> None:
        self.executed.append(command_id)
        del self.executed[:-MAX_EXECUTED]


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AgentState:
        if not self.path.exists():
            return AgentState()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["assignments"] = {k: Assignment(**v) for k, v in raw.get("assignments", {}).items()}
        raw["activations"] = {k: Activation(**v) for k, v in raw.get("activations", {}).items()}
        return AgentState(**raw)

    def save(self, state: AgentState) -> None:
        atomic_write(self.path, json.dumps(asdict(state), indent=1, sort_keys=True).encode(), 0o600)


class AuditLog:
    """Local, append-only record of every instruction from the cloud and what the agent did with
    it (design §17.4)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, event: str, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": time.time(), "event": event, **fields}, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines]
