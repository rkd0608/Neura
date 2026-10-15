"""The robot-side process for a real arm (design §7): the runtime, the session client and the
gateway connection around a connected ``So101Arm``, driven by a local operator.

Nothing moves without the operator: the process never arms on its own. Operator commands arrive
as lines (from a terminal, a pipe, or ``neura-qualify``) and run on the control loop, the one
thread that owns the servo bus:

  arm                 start a task (local activation permission)
  stop                end the task at a boundary; the arm holds, and the hold is confirmed
  takeover            revoke the policy's authority now; queued actions are discarded and the
                      arm holds where it is
  reset               after a latched fault: bring every goal to the present position and
                      torque back on, revalidate the actuator, bus, calibration and contract,
                      then reset
  instruction TEXT    the language instruction, for language-conditioned models
  status              one JSON line
  quit                end the task, close the session and exit (the arm holds, by default)

``AssignedArm`` runs whatever revision the site assigns this robot, switching only at a task
boundary, without letting go of the arm.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import grpc

from neura_contracts import ContractError, FaultResponse, RobotProfile
from neura_robots.so101 import ArmError, So101Arm
from neura_runtime.binding import ExecutionBinding
from neura_runtime.grpc_client import GatewayConnection, RobotCredentials, fetch_assignment
from neura_runtime.runtime import ACTIVE_STATES, RobotRuntime, RuntimeState, TransitionError
from neura_runtime.session import SessionClient, SessionConfig
from neura_sim.grpc_robots import binding_from_documents, watch_robot
from neura_sim.invariants import InvariantMonitor

log = logging.getLogger("neura.arm")
Say = Callable[[str], None]


class OperatorInbox:
    """Operator commands from any thread, executed on the control loop. A command older than
    ``max_age_s`` when the loop gets to it (typed while no revision was being served, say) is
    dropped, never executed late: a stale ``arm`` must not start a task."""

    def __init__(self, max_age_s: float = 2.0) -> None:
        self.max_age_s = max_age_s
        self._lines: deque[tuple[float, str]] = deque()  # append and popleft are thread-safe

    def submit(self, line: str) -> None:
        if line.strip():
            self._lines.append((time.monotonic(), line.strip()))

    def take(self) -> tuple[list[str], list[str]]:
        """(fresh, stale) commands, oldest first."""
        fresh: list[str] = []
        stale: list[str] = []
        now = time.monotonic()
        while self._lines:
            at, line = self._lines.popleft()
            (fresh if now - at <= self.max_age_s else stale).append(line)
        return fresh, stale


class ArmProcess:
    def __init__(
        self,
        arm: So101Arm,
        binding: ExecutionBinding,
        robot_id: str,
        revision_id: str,
        target: str,
        credentials: RobotCredentials | str,
        *,
        inbox: OperatorInbox | None = None,
        instruction: str | None = None,
        say: Say = print,
        sample_every: int = 1,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self.arm = arm
        self.robot_id = robot_id
        self.revision_id = revision_id
        self.runtime = RobotRuntime(binding, arm, arm)
        if binding.language_input is not None and instruction:
            self.runtime.set_instruction(instruction[: binding.language_max_bytes])
        self.client = SessionClient(
            self.runtime, SessionConfig(robot_id=robot_id, revision_id=revision_id)
        )
        self.connection = GatewayConnection(self.client, target, credentials, on_tick=self._on_tick)
        self.inbox = inbox or OperatorInbox()
        self.say = say
        self.sample_every = max(1, sample_every)
        self.on_quit = on_quit
        self.resets = 0
        self._ticks = 0
        self._last_state: RuntimeState | None = None
        self._monitor: InvariantMonitor | None = None
        self._watched: Any = None
        self.tick_hooks: list[Callable[[int], None]] = []

    def watch(self, monitor: InvariantMonitor) -> None:
        self._monitor = monitor
        self._watched = watch_robot(
            monitor, self, self.robot_id, self.client, lambda: self.arm.last_command
        )

    @property
    def violations(self) -> int:
        if self._monitor is None:
            return 0
        return self._monitor.by_client[self._watched.label]

    @property
    def at_task_boundary(self) -> bool:
        return self.runtime.state not in ACTIVE_STATES

    # --- the control loop -----------------------------------------------------------------------

    def _on_tick(self, now_us: int) -> None:
        fresh, stale = self.inbox.take()
        for line in stale:
            self.say(f"ignored {line!r}: it waited too long; type it again")
        for line in fresh:
            self.say(self.execute(line, now_us))
        self._ticks += 1
        if self._ticks % self.sample_every == 0:
            self.arm.sample(now_us)
        runtime = self.runtime
        if self._monitor is not None:
            self._monitor.ticked(
                self._watched, now_us, runtime.state, runtime.task_epoch, runtime.fault
            )
        for hook in self.tick_hooks:
            hook(now_us)
        if runtime.state is not self._last_state:
            self._last_state = runtime.state
            self.say(self._describe())

    def _describe(self) -> str:
        runtime = self.runtime
        line = f"{self.robot_id}: {runtime.state.value}"
        fault = runtime.fault
        if runtime.state is RuntimeState.FAULTED and fault is not None:
            response = fault.response.value if fault.response else "none"
            confirmed = "confirmed" if fault.response_confirmed else "NOT CONFIRMED"
            line += f" ({fault.reason.value}; {response} {confirmed})"
            if fault.detail:
                line += f": {fault.detail}"
            line += " - type reset once it is safe"
        elif runtime.state is RuntimeState.READY:
            line += " - type arm to start a task"
        return line

    # --- operator commands ----------------------------------------------------------------------

    def execute(self, line: str, now_us: int) -> str:
        word, _, rest = line.partition(" ")
        runtime = self.runtime
        try:
            if word == "arm":
                runtime.arm(now_us)
                return f"armed: task epoch {runtime.task_epoch}"
            if word == "stop":
                runtime.complete_task(now_us)
                return "stopped at a task boundary"
            if word == "takeover":
                runtime.operator_takeover(now_us)
                held = self.arm.apply_response(FaultResponse.HOLD_POSITION)
                return "taken over; holding" + ("" if held else " NOT CONFIRMED")
            if word == "reset":
                return self.reset(now_us)
            if word == "instruction":
                runtime.set_instruction(rest)
                return "instruction set"
            if word == "status":
                return json.dumps(self.status())
            if word == "quit":
                if runtime.state in ACTIVE_STATES:
                    with contextlib.suppress(TransitionError):
                        runtime.complete_task(now_us)
                if self.on_quit is not None:
                    self.on_quit()
                return "quitting"
        except (TransitionError, ValueError) as exc:
            return f"{word}: {exc}"
        return f"unknown command {word!r} (arm, stop, takeover, reset, instruction, status, quit)"

    def reset(self, now_us: int) -> str:
        """The explicit local reset of a latched fault, after revalidation (design §7)."""
        if self.runtime.state is not RuntimeState.FAULTED:
            return f"reset: nothing to reset in {self.runtime.state.value}"
        try:
            self.arm.recover()  # first: a joint whose overload tripped answers every read with
            # an error until its torque is back on
        except Exception as exc:
            return f"reset refused: the arm did not take its goals ({type(exc).__name__}: {exc})"
        problems = self.arm.revalidate()
        if problems:
            return "reset refused:\n- " + "\n- ".join(problems)
        if self._monitor is not None:
            self._monitor.reset(self._watched)
        self.runtime.reset(now_us, revalidated=True)
        self.resets += 1
        return "reset: ready"

    def status(self) -> dict[str, Any]:
        snapshot = self.runtime.snapshot()
        positions = self.arm.telemetry[-1].measured if self.arm.telemetry else {}
        return {
            "robot": self.robot_id,
            "revision": self.revision_id,
            "state": snapshot.state.value,
            "epoch": snapshot.task_epoch,
            "dispatched": self.runtime.dispatched,
            "queued": snapshot.queued_actions,
            "fault": snapshot.fault.reason.value if snapshot.fault else None,
            "late_ticks": self.connection.late_ticks,
            "rtt_p99_us": self.client.rtt.p99_us(),
            "positions": {k: round(v, 2) for k, v in positions.items()},
            "arm": dict(self.arm.counters),
            **({"violations": self.violations} if self._monitor is not None else {}),
        }


class AssignedArm:
    """The revision the site assigns this robot, over the authenticated gateway channel; a new
    assignment takes effect at the next task boundary (design §16), after the new robot profile
    is checked against the arm. A revision that does not fit is refused and the current one
    keeps running."""

    def __init__(
        self,
        open_arm: Callable[[RobotProfile], So101Arm],
        robot_id: str,
        *,
        gateway: str,
        credentials: RobotCredentials | str,
        inbox: OperatorInbox,
        instruction: str | None = None,
        say: Say = print,
        poll_seconds: float = 2.0,
    ) -> None:
        self.open_arm = open_arm  # connects the arm for the first revision's robot profile
        self.arm: So101Arm | None = None
        self.robot_id = robot_id
        self.gateway = gateway
        self.credentials = credentials
        self.inbox = inbox
        self.instruction = instruction
        self.say = say
        self.poll = poll_seconds
        self.process: ArmProcess | None = None
        self.refused: str | None = None

    async def run(self, stop: asyncio.Event) -> None:
        task: asyncio.Task[None] | None = None
        process_stop = asyncio.Event()
        while not stop.is_set():
            if self.process is None:
                fresh, stale = self.inbox.take()
                for line in [*stale, *fresh]:
                    if line == "quit":
                        stop.set()
                    else:
                        self.say(f"ignored {line!r}: no revision is assigned to this robot yet")
            try:
                assignment = await fetch_assignment(self.gateway, self.credentials)
            except (grpc.aio.AioRpcError, OSError) as exc:
                log.debug("no assignment: %s", exc)
                assignment = None
            wanted = assignment.revision_id if assignment is not None else ""
            current = self.process.revision_id if self.process is not None else None
            if wanted and wanted != current and wanted != self.refused:
                if self.process is not None and not self.process.at_task_boundary:
                    if self.refused != f"waiting:{wanted}":
                        self.say(f"revision {wanted} assigned: switching after this task")
                        self.refused = f"waiting:{wanted}"
                else:
                    assert assignment is not None
                    try:
                        documents = {r: json.loads(v) for r, v in assignment.documents.items()}
                        binding = binding_from_documents(documents)
                        if self.arm is None:
                            self.arm = self.open_arm(binding.robot)
                        else:
                            self.arm.adopt(binding.robot)
                    except (ValueError, KeyError, ContractError, ArmError) as exc:
                        self.refused = wanted
                        self.say(f"refused revision {wanted}: {exc}")
                        continue
                    if task is not None:
                        process_stop.set()
                        await task
                    self.process = ArmProcess(
                        self.arm,
                        binding,
                        self.robot_id,
                        wanted,
                        self.gateway,
                        self.credentials,
                        inbox=self.inbox,
                        instruction=self.instruction,
                        say=self.say,
                        on_quit=stop.set,
                    )
                    self.refused = None
                    self.say(f"serving revision {wanted}")
                    process_stop = asyncio.Event()
                    task = asyncio.create_task(self.process.connection.run(process_stop))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.poll)
        if task is not None:
            process_stop.set()
            await task
