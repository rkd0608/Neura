"""An incident record in Foxglove's own message types, so Foxglove's built-in panels draw it as it
opens, with nothing to configure:

- ``/neura/log`` (``foxglove.Log``): the story told by ``timeline``, a line per thing that
  happened, faults as errors and holds as warnings, for the Log panel;
- ``/neura/scene`` (``foxglove.SceneUpdate``): a status board for the 3D panel, the runtime's
  state as a coloured label, a bar per joint for the command last sent to the arm (its height the
  command between the joint's minimum and maximum), and the fault.

Foxglove knows these types by their schema name; the messages follow its published JSON schemas
(``@foxglove/schemas``). Times are nanoseconds of the wall clock, as the record's own events.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from neura_runtime.incident import Event

FRAME = "neura"
STATE_COLORS = {
    "Disconnected": (0.6, 0.63, 0.65),
    "Ready": (0.49, 0.55, 0.71),
    "Armed": (0.23, 0.51, 0.96),
    "Running": (0.09, 0.64, 0.29),
    "Waiting": (0.96, 0.62, 0.04),
    "Faulted": (0.86, 0.15, 0.15),
}
ERROR, WARNING, INFO = 4, 3, 2
WARNINGS = ("the policy gave no answer", "the arm told to", "policy answer refused")
ERRORS = ("FAULT", "control of the arm lost", "the arm refused", "refused by the runtime")


def _time(ns: int) -> dict[str, int]:
    return {"sec": ns // 1_000_000_000, "nsec": ns % 1_000_000_000}


def _pose(x: float, y: float, z: float) -> dict[str, Any]:
    return {
        "position": {"x": x, "y": y, "z": z},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    }


def _color(rgb: tuple[float, float, float], a: float = 1.0) -> dict[str, float]:
    return {"r": rgb[0], "g": rgb[1], "b": rgb[2], "a": a}


def _text(
    x: float, z: float, text: str, size: float, rgb: tuple[float, float, float]
) -> dict[str, Any]:
    return {
        "pose": _pose(x, 0.0, z),
        "billboard": True,
        "font_size": size,
        "scale_invariant": True,
        "color": _color(rgb),
        "text": text,
    }


def log_messages(events: Sequence[Event], wall_offset_ns: int) -> list[tuple[int, dict[str, Any]]]:
    """The story as ``foxglove.Log`` messages: (log time in ns, message)."""
    from neura_runtime.timeline import lines_from

    out = []
    for line in lines_from(events):
        at = line.start_us * 1000 + wall_offset_ns
        level = ERROR if line.text.startswith(ERRORS) else INFO
        if level == INFO and line.text.startswith(WARNINGS):
            level = WARNING
        repeat = f"  (x{line.count} over {(line.end_us - line.start_us) / 1e6:.2f}s)"
        message = line.text + (repeat if line.count > 1 else "")
        out.append(
            (
                at,
                {
                    "timestamp": _time(at),
                    "level": level,
                    "message": message,
                    "name": "neura",
                    "file": "",
                    "line": 0,
                },
            )
        )
    return out


def scene_messages(
    events: Sequence[Event],
    commands: Sequence[tuple[str, float, float]],
    wall_offset_ns: int,
) -> list[tuple[int, dict[str, Any]]]:
    """A ``foxglove.SceneUpdate`` each time the state changes or a command is sent: the state,
    the last command per joint (``commands``: name, minimum, maximum), the fault."""
    now = next((int(e["t"]) for e in events if "t" in e), 0)
    state, fault, action = "", "", None
    out = []
    for event in events:
        now = int(event.get("t", now))
        kind = event["kind"]
        if kind == "state":
            state = str(event["state"])
        elif kind == "fault":
            fault = f"FAULT {event['reason']}"
        elif kind == "commanded":
            action = event["action"]
        else:
            continue
        at = now * 1000 + wall_offset_ns
        out.append(
            (at, {"deletions": [], "entities": [_board(at, state, fault, action, commands)]})
        )
    return out


def _board(
    at: int,
    state: str,
    fault: str,
    action: Sequence[float] | None,
    commands: Sequence[tuple[str, float, float]],
) -> dict[str, Any]:
    width = 0.18 * max(len(commands) - 1, 0)
    cubes, texts = [], []
    for j, (name, low, high) in enumerate(commands):
        x = 0.18 * j - width / 2
        # The joint's full range, faint; the command, solid, as high as it is through the range.
        cubes.append(
            {
                "pose": _pose(x, 0.0, 0.25),
                "size": {"x": 0.1, "y": 0.1, "z": 0.5},
                "color": _color((0.5, 0.5, 0.5), 0.15),
            }
        )
        if action is not None and high > low:
            level = min(max((float(action[j]) - low) / (high - low), 0.0), 1.0)
            height = max(0.5 * level, 0.005)
            cubes.append(
                {
                    "pose": _pose(x, 0.0, height / 2),
                    "size": {"x": 0.1, "y": 0.1, "z": height},
                    "color": _color((0.15, 0.39, 0.92)),
                }
            )
        texts.append(_text(x, -0.08, name, 12, (0.6, 0.63, 0.65)))
    color = STATE_COLORS.get(state, (0.6, 0.63, 0.65))
    texts.append(_text(0.0, 0.85, state or "?", 36, color))
    if fault:
        texts.append(_text(0.0, 0.7, fault, 18, STATE_COLORS["Faulted"]))
    return {
        "timestamp": _time(at),
        "frame_id": FRAME,
        "id": "neura-status",
        "lifetime": {"sec": 0, "nsec": 0},
        "frame_locked": False,
        "metadata": [],
        "arrows": [],
        "cubes": cubes,
        "spheres": [],
        "cylinders": [],
        "lines": [],
        "triangles": [],
        "texts": texts,
        "models": [],
    }
