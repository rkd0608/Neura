"""``neura-evidence``: published policies on the scenes they were trained for, served by Neura and
by the in-process alternatives, with the same seeds.

neura-evidence closed-loop --checkpoint hub:lerobot/act_aloha_sim_transfer_cube_human \\
    --task transfer_cube --episodes 50 --out closed-loop.json
    task success over seeded episodes for each system (``--systems``):
      lerobot-eval      in process, the world waits while the policy computes (LeRobot's
                        published protocol)
      lerobot-realtime  in process, the world keeps moving while the policy computes
      neura             the full Neura path: robot runtime, gRPC, site gateway, policy workers
neura-evidence closed-loop --synthetic ...
    the same with a random-weights checkpoint of the same shape (tests the pipeline; it never
    succeeds at the task)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
import time
from importlib import metadata
from pathlib import Path
from typing import Any

SYSTEMS = ("lerobot-eval", "lerobot-realtime", "neura")


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


def reference_agreement(
    eval_info: dict[str, Any], episodes: list[dict[str, Any]]
) -> dict[str, Any]:
    """LeRobot's own ``lerobot-eval`` on gym-aloha (``eval_info.json``) against this port's
    ``lerobot-eval`` protocol, seed by seed."""
    theirs = {int(e["seed"]): bool(e["success"]) for e in eval_info["per_episode"]}
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
        "lerobot_eval_pc_success_reported": eval_info.get("aggregated", {}).get("pc_success"),
    }


def closed_loop(args: argparse.Namespace) -> int:
    from neura_robots.aloha import AlohaSim
    from neura_robots.aloha.contracts import Chunking, write
    from neura_robots.aloha.world import RealTimeWorld
    from neura_robots.evidence.local import run_eval_protocol, run_realtime
    from neura_robots.evidence.policy import LocalPolicy, checkpoint_path
    from neura_robots.evidence.results import summarize
    from neura_robots.evidence.served import ServedRun

    systems = [s for s in args.systems.split(",") if s]
    unknown = sorted(set(systems) - set(SYSTEMS))
    if unknown:
        raise SystemExit(f"unknown systems {unknown}; known: {', '.join(SYSTEMS)}")
    seeds = list(range(args.first_seed, args.first_seed + args.episodes))
    with tempfile.TemporaryDirectory(prefix="ne-") as scratch:
        work = Path(scratch)
        migrated = False
        if args.synthetic:
            from neura_robots.evidence.synthetic import write_checkpoint

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
        report: dict[str, Any] = {
            "kind": "closed-loop",
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
            "seeds": [seeds[0], seeds[-1]] if seeds else [],
            "max_steps": args.max_steps,
            "neura_chunking": {
                "actions_per_chunk": deployment.binding.profile.spec.chunk.actions_per_chunk,
                "refill_threshold_actions": chunking.refill_at,
                "merge": "replace",
            },
            "software": _versions(),
            "hardware": _hardware(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "systems": {},
        }
        if "lerobot-eval" in systems or "lerobot-realtime" in systems:
            policy = LocalPolicy(checkpoint, args.device)
            if "lerobot-eval" in systems:
                sim = AlohaSim(args.task)
                results = run_eval_protocol(
                    policy, sim, seeds, max_steps=args.max_steps, progress=_say
                )
                sim.close()
                report["systems"]["lerobot-eval"] = {
                    "summary": summarize(results),
                    "episodes": [r.to_json() for r in results],
                }
            if "lerobot-realtime" in systems:
                world = RealTimeWorld(AlohaSim(args.task)).start()
                results = run_realtime(
                    policy, world, seeds, max_steps=args.max_steps, progress=_say
                )
                world.close()
                report["systems"]["lerobot-realtime"] = {
                    "summary": summarize(results),
                    "episodes": [r.to_json() for r in results],
                }
            del policy
        if "neura" in systems:
            run = ServedRun(
                deployment, task=args.task, device=args.device, workdir=work / "site", progress=_say
            )
            results = asyncio.run(run.episodes(seeds, max_steps=args.max_steps))
            report["systems"]["neura"] = {
                "summary": summarize(results),
                "episodes": [r.to_json() for r in results],
            }
        if args.reference is not None and "lerobot-eval" in report["systems"]:
            report["reference"] = reference_agreement(
                json.loads(args.reference.read_text(encoding="utf-8")),
                report["systems"]["lerobot-eval"]["episodes"],
            )
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    for system, data in report["systems"].items():
        s = data["summary"]
        low, high = s["success_rate_95ci"]
        print(
            f"{system:18} {s['successes']}/{s['episodes']} succeeded "
            f"({s['success_rate']:.0%}, 95% CI {low:.0%}-{high:.0%})"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neura-evidence",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    loop = sub.add_parser("closed-loop", help="task success, in process and served by Neura")
    loop.add_argument("--checkpoint", help="a LeRobot checkpoint directory, or hub:<repo>")
    loop.add_argument("--synthetic", choices=["tiny", "small", "base"], help="random weights")
    loop.add_argument("--source", help="where the checkpoint comes from, for the report")
    loop.add_argument("--task", choices=["transfer_cube", "insertion"], default="transfer_cube")
    loop.add_argument("--episodes", type=int, default=50)
    loop.add_argument("--first-seed", type=int, default=1000)
    loop.add_argument("--max-steps", type=int, default=400, help="LeRobot's ALOHA episode length")
    loop.add_argument("--systems", default=",".join(SYSTEMS))
    loop.add_argument("--device", default="cpu")
    loop.add_argument("--actions-per-chunk", type=int, default=100)
    loop.add_argument("--refill-at", type=int, default=30, help="actions left when Neura refills")
    loop.add_argument(
        "--reference", type=Path, help="eval_info.json of lerobot-eval on gym-aloha, same seeds"
    )
    loop.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "closed-loop":
        return closed_loop(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
