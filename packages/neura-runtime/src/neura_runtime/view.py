"""An incident record as a web page, one self-contained HTML file with nothing to install: the
runtime's state, the policy's answers and the operator's actions on one timeline, the commands
sent to the arm plotted joint by joint, the fault, and the story in words (``timeline``).
``neura-robot view RECORD`` writes it next to the record and opens it in the browser.
"""

from __future__ import annotations

import html
import math
from pathlib import Path

from neura_runtime.replay import Record
from neura_runtime.timeline import OPERATOR, lines

WIDTH, LABELS, LANE = 1000, 150, 34
PLOT_HEIGHT = 200
STATE_COLORS = {
    "Disconnected": "#9aa0a6",
    "Ready": "#7c8db5",
    "Armed": "#3b82f6",
    "Running": "#16a34a",
    "Waiting": "#f59e0b",
    "Faulted": "#dc2626",
}
JOINT_COLORS = ["#2563eb", "#16a34a", "#f59e0b", "#db2777", "#7c3aed", "#0891b2", "#65a30d"]
STYLE = """
:root { --bg: #ffffff; --fg: #1f2328; --muted: #656d76; --grid: #e5e7eb; --panel: #f6f8fa; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --grid: #30363d; --panel: #161b22; }
}
body { background: var(--bg); color: var(--fg); margin: 0;
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 15px; margin: 28px 0 8px; }
.muted { color: var(--muted); } .fault { color: #dc2626; font-weight: 600; }
.chart { overflow-x: auto; }
svg { width: 100%; min-width: 760px; height: auto; display: block; background: var(--panel);
  border-radius: 8px; }
svg text { fill: var(--fg); font-size: 12px; } svg .muted { fill: var(--muted); }
svg .grid { stroke: var(--grid); }
pre { background: var(--panel); border-radius: 8px; padding: 12px 14px; overflow-x: auto;
  font: 12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
.legend span { display: inline-block; margin-right: 14px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  margin-right: 5px; }
"""


def _ticks(span_s: float) -> float:
    """A round step for about eight axis ticks."""
    raw = max(span_s / 8, 1e-3)
    power = 10 ** math.floor(math.log10(raw))
    return float(next(m * power for m in (1, 2, 5, 10) if m * power >= raw))


def render(record: Record, name: str = "") -> str:
    events = record.events
    times = [int(e["t"]) for e in events if "t" in e]
    first, last = (times[0], times[-1]) if times else (0, 1)
    span = max(last - first, 1)
    inner = WIDTH - LABELS - 20

    def x(t: int) -> float:
        return LABELS + (t - first) / span * inner

    # Each event's time: the last timed one before it (the first timed one, before any).
    stamped: list[tuple[int, dict[str, object]]] = []
    now = first
    for event in events:
        now = int(event.get("t", now))
        stamped.append((now, event))

    svg: list[str] = []
    lanes = ["runtime state", "policy answers", "operator", "the arm"]
    height = 30 + LANE * len(lanes) + 24
    step = _ticks(span / 1e6)
    tick = 0.0
    while tick <= span / 1e6 + 1e-9:
        tx = x(first + int(tick * 1e6))
        svg.append(f'<line class="grid" x1="{tx:.1f}" y1="22" x2="{tx:.1f}" y2="{height - 22}"/>')
        svg.append(
            f'<text class="muted" x="{tx:.1f}" y="{height - 6}" text-anchor="middle">'
            f"{tick:g}s</text>"
        )
        tick += step
    for i, lane in enumerate(lanes):
        y = 30 + LANE * i
        svg.append(f'<text x="10" y="{y + LANE / 2 + 4}">{lane}</text>')

    # The runtime's state, as bands.
    states = [(t, str(e["state"])) for t, e in stamped if e["kind"] == "state"]
    for index, (t, state) in enumerate(states):
        end = states[index + 1][0] if index + 1 < len(states) else last
        x0, x1 = x(t), max(x(end), x(t) + 2)
        color = STATE_COLORS.get(state, "#9aa0a6")
        svg.append(
            f'<rect x="{x0:.1f}" y="34" width="{x1 - x0:.1f}" height="{LANE - 8}" rx="3" '
            f'fill="{color}"><title>{state}</title></rect>'
        )
        if x1 - x0 > 8 * len(state):
            svg.append(
                f'<text x="{(x0 + x1) / 2:.1f}" y="{34 + LANE / 2}" text-anchor="middle" '
                f'style="fill:#fff">{state}</text>'
            )
    # The policy's answers: accepted, refused, not given.
    answers_y = 30 + LANE + LANE / 2
    for t, e in stamped:
        if e["kind"] == "chunk":
            color = "#16a34a" if e["accepted"] else "#dc2626"
            title = "accepted" if e["accepted"] else f"refused: {e['reason']}"
            svg.append(
                f'<circle cx="{x(t):.1f}" cy="{answers_y}" r="5" fill="{color}">'
                f"<title>{title}</title></circle>"
            )
        elif e["kind"] == "input" and e.get("call") == "on_rejected":
            svg.append(
                f'<line x1="{x(t):.1f}" y1="{answers_y - 6}" x2="{x(t):.1f}" y2="{answers_y + 6}" '
                f'stroke="#9aa0a6"><title>no answer</title></line>'
            )
    # The operator's actions, and what the arm was told.
    for t, e in stamped:
        if e["kind"] == "input" and e.get("call") in OPERATOR:
            label = str(OPERATOR[str(e["call"])]).removeprefix("operator: ")
            oy = 30 + LANE * 2 + LANE / 2
            svg.append(f'<path d="M{x(t):.1f} {oy - 7} l6 12 h-12 z" fill="#3b82f6"/>')
            svg.append(f'<text x="{x(t) + 9:.1f}" y="{oy + 4}">{html.escape(label)}</text>')
        if e["kind"] == "response_result":
            ay = 30 + LANE * 3 + LANE / 2
            label = str(e["response"]).replace("_", " ")
            svg.append(
                f'<rect x="{x(t) - 4:.1f}" y="{ay - 6}" width="8" height="12" fill="#f59e0b"/>'
            )
            svg.append(f'<text x="{x(t) + 8:.1f}" y="{ay + 4}">{html.escape(label)}</text>')
    faults = [(t, e) for t, e in stamped if e["kind"] == "fault"]
    for t, e in faults:
        svg.append(
            f'<line x1="{x(t):.1f}" y1="24" x2="{x(t):.1f}" y2="{height - 22}" stroke="#dc2626" '
            f'stroke-width="2" stroke-dasharray="4 3"/>'
        )
        svg.append(
            f'<text x="{x(t) - 4:.1f}" y="18" text-anchor="end" class="fault" '
            f'style="fill:#dc2626">FAULT {html.escape(str(e["reason"]))}</text>'
        )
    timeline_svg = f'<svg viewBox="0 0 {WIDTH} {height}" role="img">{"".join(svg)}</svg>'

    # The commands sent to the arm, each joint scaled to its range in the robot profile.
    commands = record.binding.robot.spec.command_interface
    sent = [(t, e["action"]) for t, e in stamped if e["kind"] == "commanded"]
    plot: list[str] = []
    top, bottom = 16, PLOT_HEIGHT - 24
    for fraction, label in ((0.0, "min"), (0.5, "mid"), (1.0, "max")):
        py = bottom - fraction * (bottom - top)
        plot.append(
            f'<line class="grid" x1="{LABELS}" y1="{py:.1f}" x2="{WIDTH - 20}" y2="{py:.1f}"/>'
        )
        plot.append(
            f'<text class="muted" x="{LABELS - 8}" y="{py + 4:.1f}" text-anchor="end">'
            f"{label}</text>"
        )
    legend = []
    for j, feature in enumerate(commands):
        color = JOINT_COLORS[j % len(JOINT_COLORS)]
        low, high = feature.min, feature.max
        points = " ".join(
            f"{x(t):.1f},{bottom - (float(a[j]) - low) / (high - low) * (bottom - top):.1f}"  # type: ignore[index]
            for t, a in sent
        )
        if points:
            plot.append(
                f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.6"/>'
            )
        legend.append(f'<span><i style="background:{color}"></i>{html.escape(feature.name)}</span>')
    for t, _ in faults:
        plot.append(
            f'<line x1="{x(t):.1f}" y1="{top}" x2="{x(t):.1f}" y2="{bottom}" stroke="#dc2626" '
            f'stroke-width="2" stroke-dasharray="4 3"/>'
        )
    plot_svg = f'<svg viewBox="0 0 {WIDTH} {PLOT_HEIGHT}" role="img">{"".join(plot)}</svg>'

    story = "\n".join(
        f"{(line.start_us - first) / 1e6:7.2f}s  {line.text}"
        + (
            f"  x{line.count} over {(line.end_us - line.start_us) / 1e6:.2f}s"
            if line.count > 1
            else ""
        )
        for line in lines(record)
    )
    fault_text = (
        f'<span class="fault">FAULT {html.escape(str(faults[0][1]["reason"]))}</span>'
        if faults
        else "no fault"
    )
    answers_legend = (
        '<span><i style="background:#16a34a;border-radius:50%"></i>policy answer accepted</span>'
        '<span><i style="background:#dc2626;border-radius:50%"></i>refused</span>'
        '<span><i style="background:#9aa0a6;width:2px"></i>no answer</span>'
    )
    states_legend = "".join(
        f'<span><i style="background:{c}"></i>{s}</span>' for s, c in STATE_COLORS.items()
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(name or record.robot_id)}</title><style>{STYLE}</style></head>
<body><main>
<h1>{html.escape(record.robot_id)}: {fault_text}</h1>
<div class="muted">{html.escape(name)} &middot; {span / 1e6:.2f} s recorded &middot;
{len(sent)} commands sent &middot; robot {html.escape(record.binding.robot.metadata.name)}</div>
<h2>What happened</h2>
<div class="chart">{timeline_svg}</div>
<p class="legend muted">{states_legend}{answers_legend}</p>
<h2>Commands sent to the arm</h2>
<div class="chart">{plot_svg}</div>
<p class="legend muted">{"".join(legend)} &middot; each joint from its minimum to its maximum</p>
<h2>In words</h2>
<pre>{html.escape(story)}</pre>
</main></body></html>
"""


def write(record: Record, path: Path) -> Path:
    """The page for the record at ``path``, written next to it (``.html``)."""
    out = path.with_suffix(".html")
    out.write_text(render(record, path.name), encoding="utf-8")
    return out
