"""``neura-evidence``: published policies on the scenes they were trained for, served by Neura and
by the alternatives a robotics team would otherwise use, with the same seeds and the same faults.

neura-evidence closed-loop --checkpoint hub:lerobot/act_aloha_sim_transfer_cube_human \\
    --task transfer_cube --episodes 50 --out closed-loop.json
    task success over seeded episodes for each system (``--systems``):
      lerobot-eval          in process, the world waits while the policy computes (the protocol
                            LeRobot publishes its success rates with)
      lerobot-realtime      in process, in the robot's 50 Hz loop: the world keeps moving while
                            the policy computes (lerobot-record --policy.path)
      neura                 the full Neura path: robot runtime, gRPC, site gateway, workers
      lerobot-async-scaled  LeRobot's remote inference (PolicyServer, RobotClient), its joint
                            similarity tolerance scaled to radians (see ``lerobot_async``)
      lerobot-async         the same, as shipped
neura-evidence faults --checkpoint ... --scenarios nominal,delay_400,partition,inference_kill
    the same faults (``faults``) for the remote systems
--synthetic tiny instead of --checkpoint: a random-weights checkpoint of the same shape (tests
the pipeline; it never succeeds at the task)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np

REMOTE_SYSTEMS = ("neura", "lerobot-async-scaled", "lerobot-async")
SYSTEMS = ("lerobot-eval", "lerobot-realtime", *REMOTE_SYSTEMS)


def _say(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _versions() -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for package in ("neura-robots", "lerobot", "torch", "mujoco", "numpy"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "absent"
    return versions


def _hardware() -> dict[str, Any]:
    info: dict[str, Any] = {"machine": platform.machine(), "cpus": os.cpu_count()}
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                info["cpu"] = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    try:
        import torch

        info["torch_threads"] = torch.get_num_threads()
        if torch.cuda.is_available():
            info["gpu"] = torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return info


def reference_outcomes(eval_info: dict[str, Any], first_seed: int) -> dict[int, bool]:
    """Seed -> success in ``lerobot-eval``'s ``eval_info.json``. LeRobot 0.6 lists each task's
    successes in episode order (``per_task[].metrics.successes``); with ``--eval.batch_size=1
    --seed=S`` episode i ran with seed S + i. Older releases listed episodes with their seeds."""
    if "per_episode" in eval_info:
        return {int(e["seed"]): bool(e["success"]) for e in eval_info["per_episode"]}
    successes = [
        bool(success)
        for task in eval_info.get("per_task", [])
        for success in task["metrics"]["successes"]
    ]
    return {first_seed + i: success for i, success in enumerate(successes)}


def reference_agreement(
    eval_info: dict[str, Any], episodes: list[dict[str, Any]], first_seed: int = 1000
) -> dict[str, Any]:
    """LeRobot's own ``lerobot-eval`` on gym-aloha (``eval_info.json``) against this port's
    ``lerobot-eval`` protocol, seed by seed."""
    theirs = reference_outcomes(eval_info, first_seed)
    ours = {int(e["seed"]): bool(e["success"]) for e in episodes}
    common = sorted(set(theirs) & set(ours))
    agree = [seed for seed in common if theirs[seed] == ours[seed]]
    return {
        "tool": "lerobot-eval on gym-aloha (dm_control), Python 3.12",
        "episodes_compared": len(common),
        "same_outcome": len(agree),
        "different_outcome_seeds": [seed for seed in common if seed not in agree],
        "lerobot_eval_success_rate": round(sum(theirs[s] for s in common) / len(common), 4)
        if common
        else None,
        "lerobot_eval_pc_success_reported": (
            eval_info.get("overall") or eval_info.get("aggregated") or {}
        ).get("pc_success"),
    }


@dataclass(slots=True)
class Context:
    """What every system of a run shares: the checkpoint, its contracts, the report header."""

    args: argparse.Namespace
    work: Path
    checkpoint: Path
    deployment: Any
    header: dict[str, Any]


def _prepare(args: argparse.Namespace, work: Path, kind: str) -> Context:
    from neura_bench.evidence.policy import checkpoint_path
    from neura_robots.aloha.contracts import Chunking, write

    migrated = False
    if args.synthetic:
        from neura_bench.evidence.synthetic import write_checkpoint

        checkpoint = write_checkpoint(work / "checkpoint", size=args.synthetic)
        source = f"synthetic ACT ({args.synthetic}), random weights"
    else:
        if not args.checkpoint:
            raise SystemExit("give --checkpoint (a directory or hub:<repo>) or --synthetic")
        _say(f"fetching {args.checkpoint}")
        checkpoint, migrated = checkpoint_path(args.checkpoint, work)
        source = args.source or str(args.checkpoint).removeprefix("hub:")
    chunking = Chunking(actions_per_chunk=args.actions_per_chunk, refill_at=args.refill_at)
    deployment = write(
        work / "contracts",
        checkpoint,
        name="act-aloha",
        source=source,
        task=args.task,
        chunking=chunking,
        device=args.device,
    )
    header: dict[str, Any] = {
        "kind": kind,
        "task": args.task,
        "checkpoint": {
            "source": source,
            "synthetic": bool(args.synthetic),
            # Saved before LeRobot 0.4: migrated with LeRobot's own tool (new files).
            "migrated_to_processors": migrated,
            "weights": [
                w.model_dump(mode="json") for w in deployment.binding.artifact.spec.weights
            ],
        },
        "max_steps": args.max_steps,
        "chunking": {
            "actions_per_chunk": deployment.binding.profile.spec.chunk.actions_per_chunk,
            "refill_when_left": chunking.refill_at,
            "neura_merge": "replace",
            "lerobot_async": {
                "chunk_size_threshold": round(chunking.refill_at / chunking.actions_per_chunk, 3),
                "aggregate_fn_name": "latest_only",
            },
        },
        "software": _versions(),
        "hardware": _hardware(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return Context(args, work, checkpoint, deployment, header)


def _run_system(
    ctx: Context, system: str, seeds: list[int], scenario: Any = None, tag: str = ""
) -> list[Any]:
    from neura_robots.aloha import AlohaSim
    from neura_robots.aloha.world import RealTimeWorld

    args = ctx.args
    if system in ("lerobot-eval", "lerobot-realtime"):
        from neura_bench.evidence.local import run_eval_protocol, run_realtime
        from neura_bench.evidence.policy import LocalPolicy

        policy = LocalPolicy(ctx.checkpoint, args.device)
        if system == "lerobot-eval":
            sim = AlohaSim(args.task)
            try:
                return run_eval_protocol(
                    policy, sim, seeds, max_steps=args.max_steps, progress=_say
                )
            finally:
                sim.close()
        world = RealTimeWorld(AlohaSim(args.task)).start()
        try:
            return run_realtime(policy, world, seeds, max_steps=args.max_steps, progress=_say)
        finally:
            world.close()
    if system.startswith("lerobot-async"):
        from neura_bench.evidence.faults import async_fault
        from neura_bench.evidence.lerobot_async import AsyncRun

        chunking = ctx.header["chunking"]
        world = RealTimeWorld(AlohaSim(args.task)).start()
        try:
            with AsyncRun(
                ctx.checkpoint,
                world,
                workdir=ctx.work / f"{system}{tag}",
                actions_per_chunk=chunking["actions_per_chunk"],
                chunk_size_threshold=chunking["lerobot_async"]["chunk_size_threshold"],
                similarity_atol=math.pi / 180 if system.endswith("-scaled") else None,
                progress=_say,
            ) as run:
                fault = async_fault(scenario) if scenario is not None else None
                return run.episodes(seeds, max_steps=args.max_steps, fault=fault)
        finally:
            world.close()
    if system == "neura":
        from neura_bench.evidence.faults import neura_fault
        from neura_bench.evidence.served import ServedRun

        served = ServedRun(
            ctx.deployment,
            task=args.task,
            device=args.device,
            workdir=ctx.work / f"site{tag}",
            progress=_say,
        )
        injected = neura_fault(scenario) if scenario is not None else None
        results = asyncio.run(served.episodes(seeds, max_steps=args.max_steps, fault=injected))
        if scenario is None and served.pairs:
            ctx.header.setdefault("parity", {})["neura"] = parity(ctx, served.pairs)
        return results
    raise SystemExit(f"unknown system {system!r}; known: {', '.join(SYSTEMS)}")


def parity(ctx: Context, pairs: list[Any]) -> dict[str, Any]:
    """The chunks Neura served against LeRobot alone (``predict_action_chunk`` with the
    checkpoint's processors) on the same inputs: the robot's state and camera frame as sent."""
    from neura_bench.evidence.policy import LocalPolicy

    policy = LocalPolicy(ctx.checkpoint, ctx.args.device)
    differences = []
    for state, image, served in pairs:
        reference = policy.predict_chunk(image, state)
        differences.append(float(np.max(np.abs(reference[: len(served)] - served))))
    return {
        "samples": len(differences),
        "max_abs_difference": max(differences),
        "median_abs_difference": sorted(differences)[len(differences) // 2],
        "units": "the model's action units (radians; the grippers 0-1)",
    }


def _systems(text: str, allowed: tuple[str, ...]) -> list[str]:
    systems = [s for s in text.split(",") if s]
    unknown = sorted(set(systems) - set(allowed))
    if unknown:
        raise SystemExit(f"unknown systems {unknown}; known: {', '.join(allowed)}")
    return systems


# The counters a summary line shows (medians over the episodes), when the system records them.
SHOWN_COUNTERS = (
    # Neura's robot runtime
    "chunks_accepted",
    "actions_trimmed",
    "median_observation_age_at_acceptance_ms",
    "max_observation_age_at_acceptance_ms",
    "late_ticks",
    # LeRobot's remote inference
    "actions_executed",
    "control_ticks",
    "starved_ticks",
    "chunks_received",
    "server_chunk_ms_median",
    "median_observation_age_at_arrival_ms",
    "max_observation_age_at_arrival_ms",
    "late_chunks",
    "actions_from_late_chunks",
)


def _table(rows: dict[str, Any], prefix: str = "") -> None:
    for system, data in rows.items():
        s = data["summary"]
        low, high = s["success_rate_95ci"]
        print(
            f"{prefix}{system:22} {s['successes']}/{s['episodes']} succeeded "
            f"({s['success_rate']:.0%}, 95% CI {low:.0%}-{high:.0%}); "
            f"{s['faulted_episodes']} faulted"
        )
        indent = prefix + " " * 23
        if s.get("faults"):
            print(f"{indent}faults: " + ", ".join(f"{k} x{n}" for k, n in s["faults"].items()))
        medians = s.get("counter_medians", {})
        shown = [f"{k} {medians[k]:g}" for k in SHOWN_COUNTERS if k in medians]
        if shown:
            print(f"{indent}medians: " + ", ".join(shown))
        # Neura's runtime counts every reason code it was given (refusals, timeouts, starvation).
        codes = [f"{k} {v:g}" for k, v in medians.items() if k.isupper() and v]
        if codes:
            print(f"{indent}reason codes, medians: " + ", ".join(codes))


def closed_loop(args: argparse.Namespace) -> int:
    from neura_bench.evidence.results import summarize

    systems = _systems(args.systems, SYSTEMS)
    seeds = list(range(args.first_seed, args.first_seed + args.episodes))
    with tempfile.TemporaryDirectory(prefix="ne-") as scratch:
        ctx = _prepare(args, Path(scratch), "closed-loop")
        report = {**ctx.header, "seeds": [seeds[0], seeds[-1]], "systems": {}}
        for system in systems:
            results = _run_system(ctx, system, seeds)
            report["systems"][system] = {
                "summary": summarize(results),
                "episodes": [r.to_json() for r in results],
            }
        if "parity" in ctx.header:
            report["parity"] = ctx.header["parity"]
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Kept before the comparison, so a reference it cannot read loses no episode.
        args.out.write_text(json.dumps(report, indent=1), encoding="utf-8")
        if args.reference is not None and "lerobot-eval" in report["systems"]:
            report["reference"] = reference_agreement(
                json.loads(args.reference.read_text(encoding="utf-8")),
                report["systems"]["lerobot-eval"]["episodes"],
                seeds[0],
            )
    args.out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    _table(report["systems"])
    return 0


def faults(args: argparse.Namespace) -> int:
    from neura_bench.evidence.faults import SCENARIOS
    from neura_bench.evidence.results import summarize

    systems = _systems(args.systems, REMOTE_SYSTEMS)
    names = [s for s in args.scenarios.split(",") if s]
    unknown = sorted(set(names) - set(SCENARIOS))
    if unknown:
        raise SystemExit(f"unknown scenarios {unknown}; known: {', '.join(SCENARIOS)}")
    seeds = list(range(args.first_seed, args.first_seed + args.episodes))
    with tempfile.TemporaryDirectory(prefix="nf-") as scratch:
        ctx = _prepare(args, Path(scratch), "faults")
        report = {**ctx.header, "seeds": [seeds[0], seeds[-1]], "scenarios": {}}
        for name in names:
            scenario = SCENARIOS[name]
            _say(f"--- {name}: {scenario.describe()}")
            rows: dict[str, Any] = {}
            for system in systems:
                results = _run_system(ctx, system, seeds, scenario, tag=f"-{name}")
                rows[system] = {
                    "summary": summarize(results),
                    "episodes": [r.to_json() for r in results],
                }
            report["scenarios"][name] = {"fault": scenario.describe(), "systems": rows}
            args.out.write_text(json.dumps(report, indent=1), encoding="utf-8")  # as it goes
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    for name, data in report["scenarios"].items():
        print(f"{name}: {data['fault']}")
        _table(data["systems"], prefix="  ")
    return 0


def _common(parser: argparse.ArgumentParser, systems: tuple[str, ...], episodes: int) -> None:
    parser.add_argument("--checkpoint", help="a LeRobot checkpoint directory, or hub:<repo>")
    parser.add_argument("--synthetic", choices=["tiny", "small", "base"], help="random weights")
    parser.add_argument("--source", help="where the checkpoint comes from, for the report")
    parser.add_argument("--task", choices=["transfer_cube", "insertion"], default="transfer_cube")
    parser.add_argument("--episodes", type=int, default=episodes)
    parser.add_argument("--first-seed", type=int, default=1000)
    parser.add_argument("--max-steps", type=int, default=400, help="LeRobot's ALOHA episode length")
    parser.add_argument("--systems", default=",".join(systems))
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--actions-per-chunk", type=int, default=100)
    parser.add_argument("--refill-at", type=int, default=30, help="actions left at a refill")
    parser.add_argument("--out", type=Path, required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neura-evidence",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    loop = sub.add_parser("closed-loop", help="task success for every system")
    _common(loop, SYSTEMS, 50)
    loop.add_argument(
        "--reference", type=Path, help="eval_info.json of lerobot-eval on gym-aloha, same seeds"
    )
    fault = sub.add_parser("faults", help="the same faults for every remote system")
    _common(fault, REMOTE_SYSTEMS, 10)
    fault.add_argument(
        "--scenarios", default="nominal,delay_100,delay_400,partition,inference_kill"
    )
    args = parser.parse_args(argv)
    if args.command == "closed-loop":
        return closed_loop(args)
    if args.command == "faults":
        return faults(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
