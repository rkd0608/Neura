"""The write-up of a comparison (``python -m neura_robots.compare``): a Markdown page with the
measures of both loops under every condition, a figure of what each loop told the arm against the
planned path, and what a real model did through neura-robot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from neura_robots.compare.loops import Run
from neura_robots.compare.metrics import LATE_US, Measures
from neura_robots.compare.scenarios import Condition, S


@dataclass
class Row:
    condition: Condition
    naive: Measures
    neura: Measures


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def table(rows: Sequence[Row]) -> str:
    out = [
        "| Condition | Loop | Commands more than 0.25 s late | Latest command | Longest pause | "
        "How it stopped | Record of why |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        for name, m in (("without neura", row.naive), ("**with neura**", row.neura)):
            latest = f"{m.late_max_ms:.0f} ms" if m.commands else "—"
            out.append(
                f"| {row.condition.title if name == 'without neura' else ''} | {name} | "
                f"{m.late_commands} of {m.commands} | {latest} | "
                f"{m.longest_gap_ms:.0f} ms | {m.stopped} | {_yes(m.record)} |"
            )
    return "\n".join(out)


def figure(naive: Run, neura: Run, planned: Sequence[tuple[int, float]], joint: int) -> str:
    """An SVG: one joint's commands from each loop, and the path planned for it, over time."""
    width, height, left, top, bottom = 900, 300, 60, 20, 40
    values = [v for _, v in planned] + [
        float(s.action[joint]) for run in (naive, neura) for s in run.sent
    ]
    low, high = min(values), max(values)
    span = (high - low) or 1.0

    def x(t: int) -> float:
        return left + t / (10 * S) * (width - left - 20)

    def y(value: float) -> float:
        return top + (high - value) / span * (height - top - bottom)

    def line(points: Sequence[tuple[int, float]], color: str, extra: str = "") -> str:
        coords = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in points)
        return (
            f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2" {extra}/>'
        )

    parts = [f'<rect width="{width}" height="{height}" fill="#ffffff"/>']
    for second in range(11):
        gx = x(second * S)
        grid = f'x1="{gx:.1f}" y1="{top}" x2="{gx:.1f}" y2="{height - bottom}"'
        parts.append(f'<line {grid} stroke="#e5e7eb"/>')
        label = f'x="{gx:.1f}" y="{height - 22}" font-size="12" text-anchor="middle"'
        parts.append(f'<text {label} fill="#57606a">{second}s</text>')
    parts.append(line(planned, "#9ca3af", 'stroke-dasharray="5 4"'))
    for run, color in ((naive, "#ea580c"), (neura, "#2563eb")):
        parts.append(line(_held([(s.at_us, float(s.action[joint])) for s in run.sent]), color))
    legend = [("#9ca3af", "planned path"), ("#ea580c", "without neura"), ("#2563eb", "with neura")]
    for i, (color, label) in enumerate(legend):
        lx = left + 10 + i * 150
        parts.append(f'<rect x="{lx}" y="{height - 14}" width="14" height="4" fill="{color}"/>')
        parts.append(
            f'<text x="{lx + 20}" y="{height - 8}" font-size="12" fill="#24292f">{label}</text>'
        )
    body = "".join(parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'font-family="-apple-system, Helvetica, Arial, sans-serif">{body}</svg>\n'
    )


def _held(points: Sequence[tuple[int, float]]) -> list[tuple[int, float]]:
    """Commands as the arm keeps them: each one held until the next."""
    steps: list[tuple[int, float]] = []
    for (t, v), (after, _) in zip(points, [*points[1:], (points[-1][0], 0.0)], strict=True):
        steps += [(t, v), (after, v)]
    return steps


def page(
    rows: Sequence[Row],
    figure_name: str,
    figure_condition: Condition,
    real: dict[str, object] | None,
) -> str:
    late_s = LATE_US / S
    real_part = ""
    if real:
        real_part = f"""
## A real model on real frames

[SmolVLA](https://huggingface.co/lerobot/smolvla_base) (Hugging Face's 450M-parameter
vision-language-action model) driving a real SO-101's recorded cameras and joints
([`lerobot/svla_so101_pickplace`](https://huggingface.co/datasets/lerobot/svla_so101_pickplace))
through neura-robot, on {real["machine"]}.

- **Its answer time, measured**: {real["latencies"]} (median {real["median_s"]:.2f} s). Each answer
  is a chunk of 50 actions, 1.7 s of motion at 30 Hz: by the time it arrives, its camera frame is
  older than the motion it plans.
- **Under the SmolVLA SO-101 contracts** (written for a GPU: an answer at most 150 ms old, a
  request abandoned after 200 ms), neura sent the arm **{real["commands"]} commands** in
  {real["seconds"]:.0f} s: {real["timeouts"]} requests ran out their time and no answer was used.
  {real["starved"]}
- **Contracts derived from its measured time**: {real["refusal"]}
- **Without neura**, the condition "SmolVLA on a CPU (measured)" in the table above: every chunk
  runs about {real["median_s"]:.1f} s behind what it was planned for.

```
{real["timeline"]}
```
"""
    return f"""# With and without neura-robot

The same policy drives the same simulated arm (MuJoCo Menagerie's SO-ARM100) through two loops,
under the same conditions:

- **without neura**: the loop most teams write first, and LeRobot's own synchronous loop. When its
  queue of actions is empty it takes an observation, waits for the policy, then sends the chunk's
  actions one per control period, from the first;
- **with neura**: the same policy through neura-robot's runtime, under contracts written for a GPU
  server answering in about 100 ms.

Each condition decides, for every request, how long the policy takes to answer and what goes
wrong, so both loops meet exactly the same policy. Time is simulated: the arm's physics steps once
per control period (50 Hz) and answers arrive when the condition says, so every run is
deterministic. The policy is scripted: from each observation it plans 50 actions along a smooth
path, starting from where it saw the arm, as a trained policy plans from what it saw. What is
measured does not depend on what it plans. Rerun it with `python -m neura_robots.compare`.

## What each loop sent the arm

A command is *late* when it reaches the arm after the moment it was planned for: the arm is acting
on the past. "More than {late_s:.2f} s late" counts commands planned for a moment at least a
quarter of a second gone.

{table(rows)}

What this shows:

- **With a steady GPU server, both loops are fine.** The plain loop's commands are all about one
  answer time (80-120 ms) late, which a well-tuned robot tolerates; neura's are on time, but for
  its first chunk, played from its first action because the arm stood still while it waited.
- **When answers come late (latency spikes, a network drop, a slow model), the plain loop
  executes the past.** It plays every chunk from its first action whenever it arrives: commands
  planned for half a second to seconds ago. neura drops the actions whose moment has passed,
  refuses answers older than its contracts allow, and holds the arm until a fresh one comes.
- **A crash or a takeover** stops both loops; neura also faults, holds, and leaves a record of
  why (`neura-robot timeline`, `neura-robot view`).

## The arm under latency spikes

One joint's commands against the path the policy planned ({figure_condition.what}). The plain
loop stalls while it waits, then replays old plans; neura stays on the plan.

![Commands under latency spikes](img/{figure_name})
{real_part}
## Limits of this comparison

- The arm is simulated and the policy scripted: these numbers are about timing and safety, not
  about how well a model does a task.
- The plain loop is a fair copy of the common pattern, not of every loop: LeRobot's asynchronous
  inference, for one, overlaps waiting and acting, and is compared separately.
- The latencies are modelled on a GPU server (about 100 ms, with the spikes and the drop above)
  except where measured.
"""


def write(
    out: Path,
    rows: Sequence[Row],
    figure_runs: tuple[Run, Run, Sequence[tuple[int, float]], int],
    figure_condition: Condition,
    real: dict[str, object] | None,
) -> Path:
    (out / "img").mkdir(parents=True, exist_ok=True)
    name = "comparison-spikes.svg"
    (out / "img" / name).write_text(figure(*figure_runs), encoding="utf-8")
    path = out / "comparison.md"
    path.write_text(page(rows, name, figure_condition, real), encoding="utf-8")
    return path
