"""An incident record told in words: what the operator did, what the runtime's state became, what
the policy answered (and what was refused, and why), and the fault, with the seconds since the
record starts. ``neura-robot timeline RECORD`` prints it.

Repeated lines in a row are told once with how many times and over how long ("the policy gave no
answer x51"); actions sent to the arm are counted, not listed. Records written before the runtime's
state was recorded tell the rest all the same.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from neura.v1 import dataplane_pb2
from neura_runtime.incident import Event
from neura_runtime.replay import Record

OPERATOR = {
    "arm": "operator: arm",
    "operator_takeover": "operator: takeover",
    "complete_task": "operator: stop the task",
    "reset": "operator: reset",
}


@dataclass(slots=True)
class Line:
    start_us: int
    end_us: int
    text: str
    count: int = 1


def _rejection(event: Event) -> str:
    message = dataplane_pb2.RequestRejected()
    message.ParseFromString(base64.b64decode(event["rejected"]))
    why = message.reason.message or message.reason.code
    return f"the policy gave no answer: {why}" if why else "the policy gave no answer"


def _describe(event: Event, authority: list[bool]) -> str | None:
    """The line an event tells, or None for what is only counted (ticks, actions, sensors)."""
    kind = event["kind"]
    if kind == "state":
        return f"state: {event['state']}"
    if kind == "input":
        call = event["call"]
        if call in OPERATOR:
            return OPERATOR[call]
        if call == "set_instruction":
            return f'operator: instruction "{event["text"]}"'
        if call == "attach_session":
            return "connected to the policy"
        if call == "detach_session":
            return f"disconnected from the policy ({event['reason']})"
        if call == "on_rejected":
            return _rejection(event)
        return None  # ticks and chunks: the chunk's own event tells it
    if kind == "chunk":
        if not event["accepted"]:
            return f"policy answer refused: {event['reason']}"
        late = f", {event['trimmed']} late actions dropped" if event["trimmed"] else ""
        return f"policy answer accepted{late}"
    if kind == "epoch":
        cause = f" ({event['cause']})" if event["cause"] else ""
        return f"new task (epoch {event['epoch']}){cause}"
    if kind == "fault":
        confirmed = "confirmed" if event["confirmed"] else "NOT confirmed"
        detail = f": {event['detail']}" if event["detail"] else ""
        response = str(event["response"]).replace("_", " ")
        return f"FAULT {event['reason']}{detail}; the arm: {response} ({confirmed})"
    if kind == "response_result":
        response = str(event["response"]).replace("_", " ")
        return f"the arm told to {response}" + ("" if event["ok"] else ": it did NOT")
    if kind == "command_result" and not event["ok"]:
        return f"the arm refused a command: {event['detail']}"
    if kind == "authority" and authority[0] != event["value"]:
        authority[0] = event["value"]
        return (
            "the arm is under the runtime's control"
            if event["value"]
            else "control of the arm lost"
        )
    if kind == "raised":
        return f"refused by the runtime: {event['error']}"
    return None


def lines(record: Record) -> Iterator[Line]:
    """The record's story, a line at a time, repeats folded."""
    return lines_from(record.events)


def lines_from(events: Sequence[Event]) -> Iterator[Line]:
    """The story of a record's events (the recorder's, before it is written, or a record's)."""
    # Events before the first timed one (the state the record starts in) happen at its time.
    now = next((int(e["t"]) for e in events if "t" in e), 0)
    authority = [True]  # the record starts with the runtime holding the arm, until it says not
    current: Line | None = None
    for event in events:
        now = int(event.get("t", now))
        text = _describe(event, authority)
        if text is None:
            continue
        if current is not None and current.text == text:
            current.count += 1
            current.end_us = now
            continue
        if current is not None:
            yield current
        current = Line(now, now, text)
    if current is not None:
        yield current


def tell(record: Record) -> str:
    """The whole story: one line per thing that happened, then the totals."""
    events = record.events
    first = next((int(e["t"]) for e in events if "t" in e), 0)
    out = []
    for line in lines(record):
        when = f"{(line.start_us - first) / 1e6:7.2f}s"
        repeat = ""
        if line.count > 1:
            repeat = f"  x{line.count} over {(line.end_us - line.start_us) / 1e6:.2f}s"
        out.append(f"{when}  {line.text}{repeat}")

    def count(kind: str, **match: object) -> int:
        return sum(
            1 for e in events if e["kind"] == kind and all(e.get(k) == v for k, v in match.items())
        )

    out.append(
        f"         {count('commanded')} actions sent to the arm; policy answers: "
        f"{count('chunk', accepted=True)} accepted, {count('chunk', accepted=False)} refused, "
        f"{count('input', call='on_rejected')} not given; "
        f"{count('fault')} fault{'s' if count('fault') != 1 else ''}"
    )
    return "\n".join(out)
