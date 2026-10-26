"""``neura-qualify``: qualify a robot integration on hardware (design §7.1).

neura-qualify run --arm-config ARM.yaml --artifact A --robot-profile R --execution-profile P
                  --checkpoint DIR [--device cuda:0] [--manual] --out REPORT.json
    the fault suite and open-loop parity on the arm in ARM.yaml (a neura-arm configuration:
    only its arm and cameras are used). The site's gateway and policy workers run locally,
    behind a fault proxy. The operator confirms before anything moves.
neura-qualify run --emulate --out REPORT.json
    the same on the emulated arm, with a synthetic ACT checkpoint (development and CI; an
    emulated report can never be promoted)
neura-qualify episodes ...
    the closed-loop protocol: operator-scored episodes served by Neura and by LeRobot alone,
    and a non-inferiority verdict (see ``episodes``)
neura-qualify promote --report REPORT.json --episodes EPISODES.json --evidence URL
    add the combination to the docs site's Supported table, only if both reports pass on
    hardware
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from neura_robots.qualify.episodes import add_parser as add_episodes_parser
from neura_robots.qualify.episodes import run as run_episodes
from neura_robots.qualify.harness import (
    SCENARIOS,
    Qualification,
    QualifyConfig,
    required,
    write_report,
)
from neura_robots.so101 import CameraConfig, So101Config

ROOT = Path(__file__).resolve().parents[5]
SUPPORTED = ROOT / "site" / "data" / "supported.json"


def _confirm(question: str) -> bool:
    print(f"\n{question} [y/N] ", end="", flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


@contextlib.contextmanager
def emulated(workdir: Path, *, image: tuple[int, int] = (96, 128)) -> Iterator[dict[str, Any]]:
    """An emulated arm with synthetic cameras and a synthetic ACT checkpoint matching it."""
    try:
        from neura_bench.deployments import synthetic
    except ImportError as exc:  # pragma: no cover - a development tool
        raise SystemExit("--emulate needs a Neura source checkout with neura-bench") from exc
    from neura_robots.emulator import SO101Emulator, write_calibration

    files = synthetic(workdir / "act", size="tiny", image=image)
    # Freshness budgets a shared CI runner's CPU meets at its latency tail (the example's are for
    # a GPU): admission would otherwise refuse sessions on a slow machine. A hardware run uses
    # the execution profile it qualifies, unchanged.
    profile = json.loads(files.execution_profile.read_text(encoding="utf-8"))
    profile["spec"]["freshness"] = {
        "max_observation_age_at_dispatch_us": 250_000,
        "max_observation_age_at_acceptance_us": 300_000,
        "request_timeout_us": 500_000,
    }
    files.execution_profile.write_text(json.dumps(profile, indent=1), encoding="utf-8")
    # Gravity: a joint that loses torque sags, so a hold that is not a hold shows.
    with SO101Emulator(gravity=300.0) as emulator:
        write_calibration(emulator, workdir / "calibration", "emulated")
        arm = So101Config(
            port=emulator.port,
            calibration_id="emulated",
            calibration_dir=workdir / "calibration",
            cameras={c.role: CameraConfig("synthetic") for c in files.binding.robot.spec.cameras},
            lock_dir=workdir / "locks",
        )
        yield {
            "arm": arm,
            "artifact": files.artifact,
            "robot_profile": files.robot_profile,
            "execution_profile": files.execution_profile,
            "checkpoint": files.checkpoint,
            "emulator": emulator,
        }


def _run(args: argparse.Namespace) -> int:
    scenarios = tuple(s for s in (args.scenarios or "").split(",") if s)
    unknown = [s for s in scenarios if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenarios {unknown}; known: {', '.join(SCENARIOS)}")
    with contextlib.ExitStack() as stack:
        workdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="nq-")))
        if args.emulate:
            setup = stack.enter_context(emulated(workdir))
        else:
            if not (args.arm_config and args.artifact and args.robot_profile):
                raise SystemExit(
                    "give --arm-config, --artifact, --robot-profile and "
                    "--execution-profile (or --emulate)"
                )
            from neura_robots.cli import RobotConfig

            robot = RobotConfig.load(args.arm_config)
            setup = {
                "arm": robot.so101(),
                "artifact": args.artifact.resolve(),
                "robot_profile": args.robot_profile.resolve(),
                "execution_profile": args.execution_profile.resolve(),
                "checkpoint": args.checkpoint.resolve() if args.checkpoint else None,
                "emulator": None,
            }
            if setup["checkpoint"] is None:
                print(
                    "no --checkpoint: the mock policy serves and parity is not measured; "
                    "this run cannot qualify",
                    file=sys.stderr,
                )
        config = QualifyConfig(
            **setup,
            device=args.device,
            scenarios=scenarios,
            manual=args.manual,
            nominal_s=args.nominal_seconds,
            hold_tolerance=args.hold_tolerance,
            parity_atol=args.parity_atol,
            instruction=args.instruction,
            workdir=workdir / "site",
            confirm=(lambda _: True) if args.yes or args.emulate else _confirm,
            progress=lambda line: print(line, file=sys.stderr, flush=True),
        )
        report = asyncio.run(Qualification(config).run())
    write_report(report, args.out)
    for scenario in report["scenarios"]:
        verdict = "skipped" if scenario["skipped"] else "pass" if scenario["passed"] else "FAIL"
        print(f"{verdict:8} {scenario['name']}")
        for check in scenario["checks"]:
            if not check["passed"]:
                print(f"         - {check['name']}: {check['detail']}")
    parity = report["parity"]
    if parity.get("measured"):
        print(
            f"{'pass' if parity['passed'] else 'FAIL':8} parity (max |error| "
            f"{parity['max_abs_error']:.2e} over {parity['samples']} chunks)"
        )
    else:
        print(f"{'FAIL':8} parity not measured: {parity.get('reason')}")
    print(f"\n{'PASSED' if report['passed'] else 'FAILED'}; report {args.out}")
    return 0 if report["passed"] else 1


def _promote(args: argparse.Namespace) -> int:
    report = json.loads(args.report.read_text(encoding="utf-8"))
    episodes = json.loads(args.episodes.read_text(encoding="utf-8"))
    problems = promotion_problems(report, episodes)
    if problems:
        print("not promoted:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 1
    if not args.evidence.startswith("https://"):
        print("--evidence must be an https link to the published reports", file=sys.stderr)
        return 1
    contracts = report["contracts"]
    entry = {
        "model": contracts["model"],
        "robot": contracts["robot"],
        "execution_profile": contracts["execution_profile_name"],
        "gpu": args.gpu or contracts["device"],
        "qualified_on": report["started_at"][:10],
        "evidence": args.evidence,
    }
    rows = json.loads(args.supported.read_text(encoding="utf-8"))
    key = ("model", "robot", "execution_profile", "gpu")
    rows = [r for r in rows if tuple(r[k] for k in key) != tuple(entry[k] for k in key)]
    rows.append(entry)
    args.supported.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(entry, indent=2))
    return 0


def promotion_problems(report: dict[str, Any], episodes: dict[str, Any]) -> list[str]:
    """Why these reports cannot make a combination Supported (empty: they can)."""
    problems = []
    if report.get("kind") != "neura-hardware-qualification":
        problems.append("the report is not a hardware qualification report")
        return problems
    if report.get("emulated"):
        problems.append("the report is from the emulated arm")
    if not report.get("passed"):
        problems.append("the fault suite or parity did not pass")
    ran = {s["name"] for s in report.get("scenarios", []) if not s.get("skipped")}
    missing = [name for name in required(emulated=False) if name not in ran]
    if missing:
        problems.append(
            f"scenarios did not run: {', '.join(missing)} (every scenario hardware can run "
            "must; see --manual)"
        )
    if episodes.get("kind") != "neura-closed-loop-episodes":
        problems.append("the episodes file is not a closed-loop episodes report")
    else:
        if episodes.get("emulated"):
            problems.append("the episodes are from the emulated arm")
        if not episodes.get("verdict", {}).get("non_inferior"):
            problems.append("closed-loop non-inferiority was not shown")
        if episodes.get("contracts", {}).get("artifact") != report["contracts"]["artifact"]:
            problems.append("the episodes ran a different artifact")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neura-qualify",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="the fault suite and parity")
    run.add_argument("--arm-config", type=Path)
    run.add_argument("--emulate", action="store_true")
    run.add_argument("--artifact", type=Path)
    run.add_argument("--robot-profile", type=Path)
    run.add_argument("--execution-profile", type=Path)
    run.add_argument("--checkpoint", type=Path)
    run.add_argument("--device", default="cpu")
    run.add_argument("--scenarios", help=f"comma-separated subset of: {', '.join(SCENARIOS)}")
    run.add_argument("--manual", action="store_true", help="include operator-performed faults")
    run.add_argument("--nominal-seconds", type=float, default=20.0)
    run.add_argument("--hold-tolerance", type=float, default=1.0, help="degrees")
    run.add_argument("--parity-atol", type=float, default=1e-3)
    run.add_argument("--instruction")
    run.add_argument("--yes", action="store_true", help="skip the operator's confirmation")
    run.add_argument("--out", type=Path, required=True)
    add_episodes_parser(sub)
    promote = sub.add_parser("promote", help="add a qualified combination to the docs")
    promote.add_argument("--report", type=Path, required=True)
    promote.add_argument("--episodes", type=Path, required=True)
    promote.add_argument("--evidence", required=True, help="https link to the published reports")
    promote.add_argument("--gpu", help="the GPU model (default: the report's device)")
    promote.add_argument("--supported", type=Path, default=SUPPORTED)
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("NEURA_LOG_LEVEL", "warning").upper())
    if args.command == "run":
        return _run(args)
    if args.command == "promote":
        return _promote(args)
    return run_episodes(args)


if __name__ == "__main__":
    sys.exit(main())
