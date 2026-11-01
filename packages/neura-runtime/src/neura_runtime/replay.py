"""Replaying an incident record: a fresh runtime, given the record's contracts, starting
checkpoint, inputs and the robot's recorded answers, must decide exactly what the robot's runtime
decided (design §30.2). A record that replays the same way is a regression test of the runtime;
one that does not names the first event that differs.

    neura-robot replay arm-1-20261031T120000-ACTUATOR_LOCK_LOST.mcap
"""

from __future__ import annotations

import base64
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura.v1 import dataplane_pb2
from neura_contracts import (
    ExecutionProfile,
    FaultResponse,
    ModelArtifact,
    ReasonCode,
    RobotProfile,
    load_document,
)
from neura_runtime.binding import ExecutionBinding
from neura_runtime.incident import CONTRACTS_ATTACHMENT, REPLAY_ATTACHMENT, Event, IncidentRecorder
from neura_runtime.interfaces import CommandResult, ImageFrame, SensorSnapshot
from neura_runtime.runtime import RobotRuntime

# The robot's answers, given back in order during a replay.
ANSWERS = frozenset({"authority", "command_result", "response_result", "capture"})
# Events derived from the others, for reading (incident.py): not compared.
DERIVED = frozenset({"state"})


class DivergedError(Exception):
    """The replay asked the robot something the record does not have next."""


@dataclass
class Record:
    robot_id: str
    checkpoint: dict[str, object]
    events: list[Event]
    binding: ExecutionBinding
    replayable: bool = True


@dataclass
class Outcome:
    matches: bool
    events: int
    first_difference: int | None = None  # index into the events
    recorded: Event | None = None
    replayed: Event | None = None
    counts: dict[str, int] = field(default_factory=dict)


def read(path: Path) -> Record:
    from mcap.reader import make_reader

    with path.open("rb") as stream:
        attachments = {a.name: a.data for a in make_reader(stream).iter_attachments()}
    replay = json.loads(attachments[REPLAY_ATTACHMENT])
    contracts = json.loads(attachments[CONTRACTS_ATTACHMENT])
    artifact = load_document(contracts["model_artifact"])
    robot = load_document(contracts["robot_profile"])
    profile = load_document(contracts["execution_profile"])
    assert isinstance(artifact, ModelArtifact)
    assert isinstance(robot, RobotProfile)
    assert isinstance(profile, ExecutionProfile)
    return Record(
        replay["robot_id"],
        replay["checkpoint"],
        replay["events"],
        ExecutionBinding(artifact, robot, profile),
        bool(replay.get("replayable", True)),
    )


class _Answers:
    """The robot's recorded answers, handed back in the order the runtime asks."""

    def __init__(self, events: list[Event]) -> None:
        self._left = deque(e for e in events if e["kind"] in ANSWERS)

    def take(self, kind: str) -> Event:
        if not self._left or self._left[0]["kind"] != kind:
            found = self._left[0]["kind"] if self._left else "nothing"
            raise DivergedError(f"the runtime asked for {kind}; the record has {found} next")
        return self._left.popleft()

    def has_authority(self) -> bool:
        return bool(self.take("authority")["value"])

    def command(self, action: NDArray[np.float64]) -> CommandResult:
        answer = self.take("command_result")
        return CommandResult(bool(answer["ok"]), answer["detail"])

    def apply_response(self, response: FaultResponse) -> bool:
        return bool(self.take("response_result")["ok"])

    def capture(self, now_us: int) -> SensorSnapshot | None:
        snapshot = self.take("capture")["snapshot"]
        if snapshot is None:
            return None
        images = {}
        for role, (width, height, at, *data) in snapshot["images"].items():
            frame = base64.b64decode(data[0]) if data else bytes(width * height * 3)
            images[role] = ImageFrame(width, height, frame, at)
        return SensorSnapshot(dict(snapshot["state"]), snapshot["state_time_us"], images)


def _call(runtime: RobotRuntime, event: Event) -> None:
    call = event["call"]
    if call == "tick":
        runtime.tick(event["t"])
    elif call == "on_chunk":
        chunk = dataplane_pb2.ActionChunk()
        chunk.ParseFromString(base64.b64decode(event["chunk"]))
        runtime.on_chunk(chunk, event["t"])
    elif call == "on_rejected":
        message = dataplane_pb2.RequestRejected()
        message.ParseFromString(base64.b64decode(event["rejected"]))
        runtime.on_rejected(message, event["t"])
    elif call == "attach_session":
        runtime.attach_session(event["session_id"], event["lease_generation"])
    elif call == "detach_session":
        runtime.detach_session(ReasonCode(event["reason"]))
    elif call == "set_instruction":
        runtime.set_instruction(event["text"])
    elif call == "reset":
        runtime.reset(event["t"], revalidated=event["revalidated"])
    else:  # arm, complete_task, operator_takeover
        getattr(runtime, call)(event["t"])
    runtime.outbox.clear()  # as the session client would take them


def replay(record: Record) -> Outcome:
    """Run the record's inputs through a fresh runtime; compare every event it produces."""
    answers = _Answers(record.events)
    with_images = any(
        len(frame) > 3
        for e in record.events
        if e["kind"] == "capture" and e["snapshot"] is not None
        for frame in e["snapshot"]["images"].values()
    )
    recorder = IncidentRecorder(
        Path("."), robot_id=record.robot_id, window_s=1e9, images=with_images
    )
    runtime = RobotRuntime(record.binding, recorder.controller(answers), recorder.sensors(answers))
    runtime.restore(record.checkpoint)
    recorder.attach(runtime)
    for event in (e for e in record.events if e["kind"] == "input"):
        try:
            _call(runtime, event)
        except DivergedError as error:
            recorder.record({"kind": "diverged", "detail": str(error)})
            break
        except Exception:  # noqa: S112 - the recorder logged it as "raised": compared below
            continue
    # The state events are derived from the decisions, for reading: records written before they
    # existed replay all the same.
    recorded_events = [e for e in record.events if e["kind"] not in DERIVED]
    replayed = [e for e in recorder.events()[1] if e["kind"] not in DERIVED]
    counts: dict[str, int] = {}
    for event in recorded_events:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    total = len(recorded_events)
    for index, (recorded, again) in enumerate(zip(recorded_events, replayed, strict=False)):
        if _comparable(recorded) != _comparable(again):
            return Outcome(False, total, index, recorded, again, counts)
    if len(replayed) != total:
        index = min(len(replayed), total)
        extra = replayed[index] if index < len(replayed) else None
        missing = recorded_events[index] if index < total else None
        return Outcome(False, total, index, missing, extra, counts)
    return Outcome(True, total, counts=counts)


def _comparable(event: Event) -> Any:
    # The record's own capture events carry what the sensors returned; a replay's carry what it
    # handed back, which leaves out image contents unless they were recorded.
    return json.loads(json.dumps(event))
