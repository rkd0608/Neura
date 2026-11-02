"""What is measured of a run (``compare.loops.Run``), the same for both loops: how late each command
was for the moment it was planned for, how long the arm went without a new command while the task
ran, how far one command jumped from the last, what was sent after the task should have stopped,
and whether the run left a record of why it stopped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from neura_robots.compare.loops import ARM_AT_US, Run

LATE_US = 250_000  # a command this late for its planned moment is acting on the past


@dataclass(frozen=True)
class Measures:
    commands: int
    late_p50_ms: float
    late_max_ms: float
    late_commands: int  # commands more than LATE_US late
    longest_gap_ms: float  # the arm without a new command while the task ran
    largest_jump_deg: float  # between consecutive commands, any joint
    after_stop: int  # commands sent after the task stopped (a takeover, a fault, the loop dying)
    stopped: str
    record: bool


def measure(run: Run) -> Measures:
    sent = run.sent
    late = sorted(s.at_us - s.planned_us for s in sent)
    until = run.task_until_us
    during = [s for s in sent if s.at_us < until]
    times = [ARM_AT_US, *(s.at_us for s in during), until]
    gap = max((b - a for a, b in pairwise(times)), default=0)
    jump = max(
        (float(max(abs(b.action - a.action))) for a, b in pairwise(sent)),
        default=0.0,
    )
    return Measures(
        commands=len(sent),
        late_p50_ms=late[len(late) // 2] / 1000 if late else 0.0,
        late_max_ms=late[-1] / 1000 if late else 0.0,
        late_commands=sum(1 for value in late if value > LATE_US),
        longest_gap_ms=gap / 1000,
        largest_jump_deg=math.degrees(jump),
        after_stop=sum(1 for s in sent if s.at_us > until),
        stopped=run.stopped_why or "ran to the end",
        record=run.record,
    )
