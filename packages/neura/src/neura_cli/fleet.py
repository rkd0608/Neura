"""The fleet's words: what robots and rollouts are called, in the console and from
`neura` (design §19.4, §29).

GENERATED FILE: do not edit. Source: catalog/fleet.yaml. Regenerate with `make gen`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

Tone = Literal["ok", "warn", "danger", "info", "muted"]
RobotState = Literal["running", "ready", "preparing", "waiting", "needs setup", "attention", "missing", "offline", "unknown"]

ROBOT_TONES: dict[RobotState, Tone] = {
    "running": "ok",
    "ready": "ok",
    "preparing": "info",
    "waiting": "warn",
    "needs setup": "warn",
    "attention": "danger",
    "missing": "danger",
    "offline": "muted",
    "unknown": "muted",
}


def robot_state(robot: Mapping[str, Any]) -> RobotState:
    """What a robot is doing, in one word: the word the console shows for it."""
    status = robot["status"]
    phase = status.get("phase") or "Unknown"
    if status.get("discovered") and status.get("present") is False:
        return "missing"
    if phase == "Offline":
        return "offline"
    if phase == "NeedsAttention" and status.get("discovered"):
        return "needs setup"
    if phase == "NeedsAttention":
        return "attention"
    if phase == "Waiting":
        return "waiting"
    if status.get("policy_version_id"):
        return "running"
    if phase == "Running":
        return "running"
    if phase == "Ready":
        return "ready"
    if phase == "Preparing":
        return "preparing"
    return "unknown"


TERMINAL = frozenset({"live", "stopped", "cancelled"})

ROLLOUT_LABELS: dict[str, str] = {
    "checking": "checking",
    "preparing": "preparing",
    "shadow_testing": "shadow testing",
    "awaiting_approval": "awaiting approval",
    "switching": "switching",
    "live": "live",
    "stopped": "stopped",
    "cancelled": "cancelled",
}

ROLLOUT_TONES: dict[str, Tone] = {
    "checking": "info",
    "preparing": "info",
    "shadow_testing": "info",
    "awaiting_approval": "warn",
    "switching": "info",
    "live": "ok",
    "stopped": "danger",
    "cancelled": "muted",
}

STEP_LABELS: dict[str, str] = {
    "check": "Fits the robots",
    "prepare": "At the site",
    "shadow": "Shadow test",
    "approve": "Approval",
    "switch": "Switch",
}

STEP_TONES: dict[str, Tone] = {
    "pending": "muted",
    "running": "info",
    "passed": "ok",
    "skipped": "muted",
    "failed": "danger",
}

STEP_MARKS: dict[str, str] = {
    "pending": " ",
    "running": "◐",
    "passed": "✓",
    "skipped": "–",
    "failed": "✗",
}

ROLLOUT_KINDS: dict[str, str] = {
    "deploy": "Deploy",
    "try": "Try",
    "rollback": "Rollback",
}
