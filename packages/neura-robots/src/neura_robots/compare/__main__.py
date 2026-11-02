"""``python -m neura_robots.compare [--out DIR] [--smolvla CONTRACTS]``: run both loops under every
condition and write ``DIR/comparison.md`` (default ``docs``) with its figure. ``--smolvla`` also
runs the real model (``compare.smolvla``: the lerobot extra, about 3 GB fetched once) with the
SmolVLA SO-101 contracts in ``CONTRACTS``, and adds what it did.
"""

from __future__ import annotations

import argparse
import asyncio
import platform
from pathlib import Path

from neura_robots.compare import loops, metrics, report, scenarios
from neura_robots.compare.scenarios import S


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path("docs"))
    parser.add_argument("--smolvla", type=Path, metavar="CONTRACTS")
    parser.add_argument("--seconds", type=float, default=8.0, help="the real model's drive")
    args = parser.parse_args()

    conditions = list(scenarios.CONDITIONS)
    real: dict[str, object] | None = None
    if args.smolvla is not None:
        from neura_robots.compare import smolvla

        model = smolvla.SmolVLA()
        machine = f"{platform.machine()} {model.device.upper()} ({platform.system()})"
        latencies = smolvla.measure(model)
        conditions.append(scenarios.measured_condition(latencies, machine))
        outcome = asyncio.run(smolvla.through_neura(model, args.smolvla, args.seconds))
        median = sorted(latencies)[len(latencies) // 2]
        scene, _ = loops._world()
        refusal = loops.refusal(scene, max(latencies))
        starved = outcome.counters.get("OBSERVATION_INCOMPLETE", 0)
        real = {
            "machine": machine,
            "latencies": ", ".join(f"{v / S:.2f} s" for v in latencies),
            "median_s": median / S,
            "commands": outcome.commands,
            "seconds": outcome.seconds,
            "timeouts": outcome.counters.get("WORKER_TIMED_OUT", 0),
            "starved": (
                f"The model also took the machine's cores from the robot's own sensing: "
                f"{starved} times the camera frames were too old to build an observation from, "
                "so none was sent. A model on the robot's own computer competes with it."
                if starved
                else ""
            ),
            "refusal": (
                f"neura refuses to start before the arm moves: {refusal}."
                if refusal
                else "they fit"
            ),
            "timeline": outcome.timeline.strip("\n"),
        }

    rows = []
    figure = None
    for condition in conditions:
        naive, neura = loops.naive(condition), loops.neura(condition)
        rows.append(report.Row(condition, metrics.measure(naive), metrics.measure(neura)))
        print(
            f"{condition.title}: {rows[-1].naive.late_commands} late without, "
            f"{rows[-1].neura.late_commands} with",
            flush=True,
        )
        if condition.name == "spikes":
            scene, _ = loops._world()
            policy = loops.Policy(scene)
            planned = [(t, float(policy.at(t)[0])) for t in range(0, 10 * S, 20_000)]
            figure = (naive, neura, planned, 0)
            figure_condition = condition
    assert figure is not None
    path = report.write(args.out, rows, figure, figure_condition, real)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
