"""The incident record: what the robot's runtime saw, decided and did, written as MCAP when a fault
latches or on request, and replayable to the same decisions (design §30.2).

``IncidentRecorder`` sits at the runtime's edges and keeps one ordered list of events:

- inputs: every control tick, chunk and rejection that reaches the runtime, session changes and
  operator commands (``arm``, ``takeover``, ...), with the robot clock's time;
- the robot's answers: authority, each command's result, each fault response, and what the sensors
  returned (joint states and image sizes; image contents only when asked for);
- decisions: each action commanded, each chunk merged or refused and why, epochs, faults, and each
  observation requested.

The list starts at a checkpoint (``RobotRuntime.checkpoint``), taken whenever a task epoch
advances, so a record covers about the last ``window_s`` seconds and always begins where the
runtime's state is known. ``neura_runtime.replay`` feeds a record's inputs and answers to a fresh
runtime and checks that it decides the same. Files open in Foxglove and Rerun: one JSON topic per
kind of event; the replay data and the contracts are attachments. Nothing leaves the machine.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_contracts import FaultResponse, ReasonCode, dump_document
from neura_runtime.interfaces import CommandResult, SensorSnapshot
from neura_runtime.runtime import FaultRecord, RobotRuntime, SendObservation

log = logging.getLogger("neura.incident")

Event = dict[str, Any]
REPLAY_ATTACHMENT = "neura-replay.json"
CONTRACTS_ATTACHMENT = "neura-contracts.json"
# How long after a fault the record keeps going before it is written: the hold and what follows.
AFTER_FAULT_US = 1_000_000
# A task that runs this long without an epoch advancing loses its start: the record keeps the
# newest events and is marked unreplayable rather than grow without bound.
MAX_EVENTS = 500_000


def proto(message: Any) -> str:
    return base64.b64encode(message.SerializeToString()).decode()


class _Controller:
    def __init__(self, inner: Any, record: Callable[[Event], None]) -> None:
        self._inner, self.record = inner, record

    def has_authority(self) -> bool:
        answer = bool(self._inner.has_authority())
        self.record({"kind": "authority", "value": answer})
        return answer

    def command(self, action: NDArray[np.float64]) -> CommandResult:
        result: CommandResult = self._inner.command(action)
        self.record({"kind": "command_result", "ok": result.ok, "detail": result.detail})
        return result

    def apply_response(self, response: FaultResponse) -> bool:
        confirmed = bool(self._inner.apply_response(response))
        self.record({"kind": "response_result", "response": response.value, "ok": confirmed})
        return confirmed


class _Sensors:
    def __init__(self, inner: Any, record: Callable[[Event], None], images: bool) -> None:
        self._inner, self.record, self._images = inner, record, images

    def capture(self, now_us: int) -> SensorSnapshot | None:
        snapshot: SensorSnapshot | None = self._inner.capture(now_us)
        event: Event = {"kind": "capture", "snapshot": None}
        if snapshot is not None:
            event["snapshot"] = {
                "state": dict(snapshot.state),
                "state_time_us": snapshot.state_time_us,
                "images": {
                    role: [f.width, f.height, f.capture_time_us]
                    + ([base64.b64encode(f.data).decode()] if self._images else [])
                    for role, f in snapshot.images.items()
                },
            }
        self.record(event)
        return snapshot


class IncidentRecorder:
    """Records one runtime. Build its controller and sensors first, give them to the runtime,
    then ``attach`` it::

        recorder = IncidentRecorder(directory, robot_id="arm-1")
        runtime = RobotRuntime(binding, recorder.controller(arm), recorder.sensors(arm))
        recorder.attach(runtime)
    """

    def __init__(
        self,
        directory: Path,
        *,
        robot_id: str,
        window_s: float = 30.0,
        images: bool = False,
        on_written: Callable[[Path], None] | None = None,
    ) -> None:
        self.directory = directory
        self.robot_id = robot_id
        self.window_us = int(window_s * 1e6)
        self.images = images
        self.on_written = on_written
        self.written: list[Path] = []
        self._events: deque[Event] = deque()
        self._checkpoints: deque[tuple[int, int, dict[str, object]]] = deque()  # (t, offset, cp)
        self._dropped = 0  # events dropped from the front, so offsets stay valid
        self._now = 0
        self._write_at: int | None = None
        self._fault: FaultRecord | None = None
        self.replayable = True
        self._writers: list[threading.Thread] = []
        self.runtime: RobotRuntime | None = None

    # --- wiring

    def controller(self, inner: Any) -> _Controller:
        return _Controller(inner, self.record)

    def sensors(self, inner: Any) -> _Sensors:
        return _Sensors(inner, self.record, self.images)

    def attach(self, runtime: RobotRuntime) -> None:
        self.runtime = runtime
        self._checkpoint()
        runtime.observers.append(self)

        def wrap(name: str, describe: Callable[..., Event]) -> None:
            original = getattr(runtime, name)

            def recorded(*args: Any, **kwargs: Any) -> Any:
                event = describe(*args, **kwargs)
                if "t" in event:
                    self._now = event["t"]
                epoch = runtime.task_epoch
                self.record({"kind": "input", "call": name, **event})
                try:
                    result = original(*args, **kwargs)
                except Exception as error:  # the runtime's refusal is part of the record
                    self.record({"kind": "raised", "error": type(error).__name__})
                    raise
                self._outbox()
                if runtime.task_epoch != epoch:
                    self._checkpoint()
                if name == "tick":
                    self._maybe_write()
                return result

            setattr(runtime, name, recorded)

        wrap("tick", lambda now_us: {"t": now_us})
        wrap("on_chunk", lambda chunk, now_us: {"t": now_us, "chunk": proto(chunk)})
        wrap("on_rejected", lambda message, now_us: {"t": now_us, "rejected": proto(message)})
        wrap("attach_session", lambda s, g: {"session_id": s, "lease_generation": g})
        wrap("detach_session", lambda reason: {"reason": reason.value})
        wrap("set_instruction", lambda text: {"text": text})
        wrap("arm", lambda now_us: {"t": now_us})
        wrap("complete_task", lambda now_us: {"t": now_us})
        wrap("operator_takeover", lambda now_us: {"t": now_us})
        wrap("reset", lambda now_us, *, revalidated: {"t": now_us, "revalidated": revalidated})

    # --- the runtime's decisions (RuntimeObserver)

    def on_command(
        self, now_us: int, task_epoch: int, index: int, action: NDArray[np.float64]
    ) -> None:
        self.record(
            {"kind": "commanded", "epoch": task_epoch, "index": index, "action": action.tolist()}
        )

    def on_chunk(
        self,
        now_us: int,
        task_epoch: int,
        sequence: int,
        start_index: int,
        accepted: bool,
        reason: ReasonCode | None,
        trimmed: int,
    ) -> None:
        self.record(
            {
                "kind": "chunk",
                "epoch": task_epoch,
                "sequence": sequence,
                "start": start_index,
                "accepted": accepted,
                "reason": reason.value if reason else None,
                "trimmed": trimmed,
            }
        )

    def on_epoch(self, previous: int, current: int, cause: ReasonCode | None) -> None:
        self.record({"kind": "epoch", "epoch": current, "cause": cause.value if cause else None})

    def on_fault(self, fault: FaultRecord) -> None:
        self.record(
            {
                "kind": "fault",
                "reason": fault.reason.value,
                "response": fault.response.value if fault.response else None,
                "confirmed": fault.response_confirmed,
                "detail": fault.detail,
            }
        )
        self._fault = fault
        self._write_at = fault.at_us + AFTER_FAULT_US

    # --- the buffer

    def record(self, event: Event) -> None:
        """Add an event to the record (the wrappers and the robot's answers do)."""
        self._events.append(event)
        if len(self._events) > MAX_EVENTS:
            self._events.popleft()
            self._dropped += 1
            self.replayable = False  # its checkpoint's events are gone

    def _outbox(self) -> None:
        assert self.runtime is not None
        for item in self.runtime.outbox:
            if isinstance(item, SendObservation):
                message = item.message
                self.record(
                    {
                        "kind": "observation",
                        "epoch": message.envelope.task_epoch,
                        "sequence": message.envelope.sequence,
                        "anchor": message.anchor_index,
                    }
                )

    def _checkpoint(self) -> None:
        assert self.runtime is not None
        offset = self._dropped + len(self._events)
        self._checkpoints.append((self._now, offset, self.runtime.checkpoint()))
        # Keep the newest checkpoint older than the window: the record starts there.
        while len(self._checkpoints) > 1 and self._checkpoints[1][0] <= self._now - self.window_us:
            self._checkpoints.popleft()
        start = self._checkpoints[0][1]
        while self._dropped < start:
            self._events.popleft()
            self._dropped += 1

    def events(self) -> tuple[dict[str, object], list[Event]]:
        """The record so far: its starting checkpoint and the events since."""
        _, offset, checkpoint = self._checkpoints[0]
        return checkpoint, list(self._events)[max(0, offset - self._dropped) :]

    # --- writing

    def _maybe_write(self) -> None:
        if self._write_at is not None and self._now >= self._write_at:
            self._write_at = None
            fault = self._fault
            name = fault.reason.value if fault else "request"
            self.write_soon(name)

    def close(self) -> None:
        """Before the robot's process ends: write a record a fault is still waiting on, and wait
        for every write to finish."""
        if self._write_at is not None:
            self._write_at = None
            self.write_soon(self._fault.reason.value if self._fault else "request")
        for writer in self._writers:
            writer.join()

    def write_soon(self, label: str = "request") -> None:
        """Write the record now, on a thread of its own: the control loop does not wait."""
        assert self.runtime is not None
        checkpoint, events = self.events()
        replayable = self.replayable
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        path = self.directory / f"{self.robot_id}-{stamp}-{label}.mcap"
        binding = self.runtime.binding
        contracts = {
            "model_artifact": dump_document(binding.artifact),
            "robot_profile": dump_document(binding.robot),
            "execution_profile": dump_document(binding.profile),
        }

        def write() -> None:
            try:
                write_mcap(
                    path, self.robot_id, checkpoint, events, contracts, replayable=replayable
                )
            except OSError as error:
                log.error("could not write the incident record %s: %s", path, error)
                return
            self.written.append(path)
            if self.on_written is not None:
                self.on_written(path)

        writer = threading.Thread(target=write, name="incident-writer", daemon=True)
        self._writers.append(writer)
        writer.start()


def write_mcap(
    path: Path,
    robot_id: str,
    checkpoint: dict[str, object],
    events: list[Event],
    contracts: dict[str, str],
    *,
    replayable: bool = True,
) -> None:
    from mcap.writer import Writer

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        writer = Writer(stream)
        writer.start(profile="", library="neura-robot")
        schema = writer.register_schema(
            name="neura.Event", encoding="jsonschema", data=b'{"type": "object"}'
        )
        channels: dict[str, int] = {}
        now = 0
        for sequence, event in enumerate(events):
            now = int(event.get("t", now))
            kind = str(event["kind"])
            topic = f"/neura/{kind}" if kind != "input" else f"/neura/input/{event['call']}"
            if topic not in channels:
                channels[topic] = writer.register_channel(
                    topic=topic, message_encoding="json", schema_id=schema
                )
            writer.add_message(
                channel_id=channels[topic],
                log_time=now * 1000,
                publish_time=now * 1000,
                sequence=sequence,
                data=json.dumps(event).encode(),
            )
        replay: dict[str, Any] = {"robot_id": robot_id, "checkpoint": checkpoint, "events": events}
        replay["replayable"] = replayable
        for name, data in (
            (REPLAY_ATTACHMENT, json.dumps(replay).encode()),
            (CONTRACTS_ATTACHMENT, json.dumps(contracts).encode()),
        ):
            writer.add_attachment(
                create_time=now * 1000,
                log_time=now * 1000,
                name=name,
                media_type="application/json",
                data=data,
            )
        writer.finish()  # type: ignore[no-untyped-call]
