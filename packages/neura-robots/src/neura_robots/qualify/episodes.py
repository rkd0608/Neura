"""Closed-loop parity: operator-scored episodes served by Neura and by LeRobot alone (design §7.1).

The same arm, checkpoint and task, two ways:

- **neura**: the site's gateway and policy worker serve the arm through ``neura-arm``'s process,
  as in production (the gateway runs locally, as in ``neura-qualify run``);
- **local**: LeRobot's own control loop on the robot computer, as ``lerobot-record
  --policy.path`` runs a policy: each control period, read the arm, ``predict_action``, send it.

Episodes alternate in balanced blocks (ABBA), so slow drift in the scene, lighting or the
operator does not favor either arm. The operator resets the scene before each episode and scores
it after, without being told which way it was served unless they ask for it (``--unblinded``).
The verdict is non-inferiority: the lower bound of the one-sided 95% confidence interval for the
difference in success rates (Neura minus local; Newcombe's hybrid score interval) must be above
minus the margin. With both arms at 90% success, 30 episodes each give a lower bound of about
-0.14, inside the default margin of 0.15; 10 episodes each cannot show it even at 100%.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import random
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from neura_contracts import FaultResponse
from neura_robots.process import ArmProcess
from neura_robots.qualify.harness import write_report
from neura_robots.qualify.site import REVISION, ROBOT_ID, TOKEN, LocalSite
from neura_robots.so101 import So101Arm, So101Config
from neura_runtime.binding import ExecutionBinding
from neura_runtime.grpc_client import monotonic_us
from neura_runtime.runtime import RuntimeState, TransitionError
from neura_sim.grpc_robots import load_binding

Z_95_ONE_SIDED = 1.6448536269514722


def wilson(successes: int, n: int, z: float = Z_95_ONE_SIDED) -> tuple[float, float]:
    """Wilson score interval for a proportion."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    denominator = 1 + z * z / n
    return (centre - spread) / denominator, (centre + spread) / denominator


def newcombe_lower(s1: int, n1: int, s2: int, n2: int, z: float = Z_95_ONE_SIDED) -> float:
    """Lower confidence bound for p1 - p2 (Newcombe 1998, method 10: hybrid score)."""
    p1, p2 = s1 / n1, s2 / n2
    l1, _ = wilson(s1, n1, z)
    _, u2 = wilson(s2, n2, z)
    return p1 - p2 - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)


def schedule(pairs: int, seed: int) -> list[str]:
    """ABBA blocks, the first arm of each block drawn at random."""
    rng = random.Random(seed)  # noqa: S311 - an episode order, not a secret
    order: list[str] = []
    while len(order) < 2 * pairs:
        a, b = ("neura", "local") if rng.random() < 0.5 else ("local", "neura")
        order += [a, b, b, a]
    return order[: 2 * pairs]


@dataclass(slots=True)
class EpisodesConfig:
    arm: So101Config
    artifact: Path
    robot_profile: Path
    execution_profile: Path
    checkpoint: Path
    device: str = "cpu"
    pairs: int = 30  # episodes per arm
    seconds: float = 30.0  # the longest an episode may run
    margin: float = 0.15  # non-inferiority margin on the success rate
    seed: int = 0
    instruction: str | None = None
    blinded: bool = True
    emulated: bool = False
    workdir: Path | None = None
    # The operator: scene reset before, and the score after each episode. Return True to go on.
    prepare: Callable[[int, str | None], bool] = lambda _i, _arm: True
    score: Callable[[int, str | None], bool] = lambda _i, _arm: False
    progress: Callable[[str], None] = lambda _: None


@dataclass(slots=True)
class Episode:
    index: int
    arm: str
    success: bool
    seconds: float
    actions: int
    fault: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Episodes:
    def __init__(self, config: EpisodesConfig) -> None:
        self.config = config
        self.binding: ExecutionBinding = load_binding(
            config.artifact, config.robot_profile, config.execution_profile
        )
        names = [f.name for f in self.binding.artifact.spec.outputs.features]
        commands = [c.name for c in self.binding.robot.spec.command_interface]
        if sorted(names) != sorted(commands):
            raise ValueError(
                "the local baseline sends the policy's actions to the arm by name, as LeRobot "
                f"does: the artifact's actions {names} are not the arm's commands {commands}"
            )
        self._order = [names.index(c) for c in commands]
        self.episodes: list[Episode] = []

    async def run(self) -> dict[str, Any]:
        from neura_robots.qualify.reference import LeRobotReference

        config = self.config
        started = time.time()
        reference = LeRobotReference(
            self.binding.artifact, self.binding.robot, config.checkpoint, device=config.device
        )
        with contextlib.ExitStack() as stack:
            workdir = config.workdir or Path(
                stack.enter_context(tempfile.TemporaryDirectory(prefix="ne-"))
            )
            site = LocalSite(
                config.artifact,
                config.robot_profile,
                config.execution_profile,
                config.checkpoint,
                config.device,
                workdir,
            )
            arm = So101Arm(config.arm, self.binding.robot)
            arm.connect()
            try:
                async with site:
                    # Unscored: the site's worker and the local policy both warm up first, so
                    # neither arm's first episode pays for loading or the first inference.
                    if not config.prepare(0, None):
                        return self.report(started)
                    await self._warm_up(arm, site.target, reference)
                    for index, which in enumerate(schedule(config.pairs, config.seed), 1):
                        shown = None if config.blinded else which
                        if not config.prepare(index, shown):
                            break
                        if which == "neura":
                            episode = await self._neura(arm, site.target, index)
                        else:
                            episode = await self._local(arm, reference, index)
                        episode.success = bool(config.score(index, shown))
                        self.episodes.append(episode)
                        config.progress(
                            f"episode {index}: {'success' if episode.success else 'failure'}"
                        )
            finally:
                arm.close()
        return self.report(started)

    async def _warm_up(self, arm: So101Arm, target: str, reference: Any) -> None:
        snapshot = arm.capture(monotonic_us())
        if snapshot is not None:
            reference.chunk(snapshot, self.config.instruction or "")
        seconds, self.config.seconds = self.config.seconds, 3.0
        try:
            episode = await self._neura(arm, target, 0, actions=15)
        finally:
            self.config.seconds = seconds
        if episode.actions == 0:
            raise RuntimeError("the site served no action during the warm-up; see its log")
        self.config.progress(f"warmed up: {episode.actions} actions served")

    async def _neura(self, arm: So101Arm, target: str, index: int, actions: int = 0) -> Episode:
        config = self.config
        process = ArmProcess(
            arm,
            self.binding,
            ROBOT_ID,
            REVISION,
            target,
            TOKEN,
            instruction=config.instruction,
            say=config.progress,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(process.connection.run(stop))
        runtime = process.runtime
        try:
            deadline = time.monotonic() + 180
            while runtime.state is not RuntimeState.READY:
                if time.monotonic() > deadline:
                    raise RuntimeError("no session with the local gateway")
                await asyncio.sleep(0.05)
            begin = time.monotonic()
            runtime.arm(monotonic_us())
            while time.monotonic() - begin < config.seconds or runtime.dispatched < actions:
                if runtime.fault is not None or time.monotonic() - begin > 180:
                    break
                await asyncio.sleep(0.05)
            seconds = time.monotonic() - begin
            fault = runtime.fault.reason.value if runtime.fault else None
            with contextlib.suppress(TransitionError):
                runtime.complete_task(monotonic_us())  # holds, confirmed
            return Episode(index, "neura", False, round(seconds, 2), runtime.dispatched, fault)
        finally:
            stop.set()
            await task

    async def _local(self, arm: So101Arm, reference: Any, index: int) -> Episode:
        config = self.config
        period = self.binding.control_period_us / 1e6
        reference.reset()
        begin = time.monotonic()
        next_tick = begin
        actions, late = 0, 0
        fault = None
        while time.monotonic() - begin < config.seconds:
            snapshot = arm.capture(monotonic_us())
            if snapshot is None or len(snapshot.images) < len(self.binding.robot.spec.cameras):
                fault = "sensors unavailable"
                break
            action = reference.select_action(snapshot, config.instruction or "")
            result = arm.command(np.asarray(action[self._order], dtype=np.float64))
            if not result.ok:
                fault = f"command failed: {result.detail}"
                break
            actions += 1
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay < 0:
                late += 1
                next_tick = time.monotonic()
            else:
                await asyncio.sleep(delay)  # LeRobot's loop sleeps out the period the same way
        seconds = time.monotonic() - begin
        arm.apply_response(FaultResponse.HOLD_POSITION)
        return Episode(index, "local", False, round(seconds, 2), actions, fault, {"late": late})

    def report(self, started: float) -> dict[str, Any]:
        config = self.config
        by = {arm: [e for e in self.episodes if e.arm == arm] for arm in ("neura", "local")}
        s_n, n_n = sum(e.success for e in by["neura"]), len(by["neura"])
        s_l, n_l = sum(e.success for e in by["local"]), len(by["local"])
        complete = n_n == n_l == config.pairs
        lower = newcombe_lower(s_n, n_n, s_l, n_l) if n_n and n_l else None
        return {
            "kind": "neura-closed-loop-episodes",
            "version": 1,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "emulated": config.emulated,
            "contracts": {
                "model": self.binding.artifact.metadata.name,
                "artifact": self.binding.artifact_digest,
                "execution_profile": self.binding.execution_profile_digest,
            },
            "protocol": {
                "pairs": config.pairs,
                "seconds": config.seconds,
                "margin": config.margin,
                "seed": config.seed,
                "blinded": config.blinded,
                "order": "ABBA blocks",
                "baseline": "LeRobot predict_action at the control rate on the same arm",
            },
            "episodes": [
                {
                    "index": e.index,
                    "arm": e.arm,
                    "success": e.success,
                    "seconds": e.seconds,
                    "actions": e.actions,
                    "fault": e.fault,
                    **e.extra,
                }
                for e in self.episodes
            ],
            "verdict": {
                "neura": {"successes": s_n, "episodes": n_n},
                "local": {"successes": s_l, "episodes": n_l},
                "difference": (s_n / n_n - s_l / n_l) if n_n and n_l else None,
                "lower_bound_95": lower,
                "non_inferior": bool(complete and lower is not None and lower > -config.margin),
                "complete": complete,
            },
        }


# --- the command line -----------------------------------------------------------------------------


def add_parser(sub: Any) -> None:
    episodes = sub.add_parser("episodes", help="the closed-loop, operator-scored protocol")
    episodes.add_argument("--arm-config", type=Path)
    episodes.add_argument("--emulate", action="store_true")
    episodes.add_argument("--artifact", type=Path)
    episodes.add_argument("--robot-profile", type=Path)
    episodes.add_argument("--execution-profile", type=Path)
    episodes.add_argument("--checkpoint", type=Path)
    episodes.add_argument("--device", default="cpu")
    episodes.add_argument("--pairs", type=int, default=30, help="episodes per arm")
    episodes.add_argument("--seconds", type=float, default=30.0, help="longest episode")
    episodes.add_argument("--margin", type=float, default=0.15)
    episodes.add_argument("--seed", type=int, default=0)
    episodes.add_argument("--instruction")
    episodes.add_argument("--unblinded", action="store_true", help="tell the operator the arm")
    episodes.add_argument(
        "--scores", help="scripted scores, e.g. yynny... (with --emulate; development only)"
    )
    episodes.add_argument("--out", type=Path, required=True)


def _ask(question: str) -> str:
    print(f"\n{question} ", end="", flush=True)
    return sys.stdin.readline().strip().lower()


def run(args: argparse.Namespace) -> int:
    from neura_robots.qualify.cli import emulated

    with contextlib.ExitStack() as stack:
        workdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="ne-")))
        if args.emulate:
            setup = stack.enter_context(emulated(workdir))
            setup.pop("emulator")
            scripted = list(args.scores or "")

            def prepare(i: int, arm: str | None) -> bool:
                return True

            def score(i: int, arm: str | None) -> bool:
                return scripted[i - 1] == "y" if i <= len(scripted) else False

        else:
            if not (args.arm_config and args.artifact and args.robot_profile and args.checkpoint):
                raise SystemExit(
                    "give --arm-config, --artifact, --robot-profile, "
                    "--execution-profile and --checkpoint (or --emulate)"
                )
            from neura_robots.cli import RobotConfig

            setup = {
                "arm": RobotConfig.load(args.arm_config).so101(),
                "artifact": args.artifact.resolve(),
                "robot_profile": args.robot_profile.resolve(),
                "execution_profile": args.execution_profile.resolve(),
                "checkpoint": args.checkpoint.resolve(),
            }

            def prepare(i: int, arm: str | None) -> bool:
                which = f" ({arm})" if arm else ""
                name = f"Episode {i}{which}" if i else "Warm-up (not scored; the arm moves)"
                reply = _ask(f"{name}: reset the scene, then press Enter (q quits).")
                return reply != "q"

            def score(i: int, arm: str | None) -> bool:
                while True:
                    reply = _ask(f"Episode {i}: success? [y/n]")
                    if reply in ("y", "n"):
                        return reply == "y"

        config = EpisodesConfig(
            **setup,
            device=args.device,
            pairs=args.pairs,
            seconds=args.seconds,
            margin=args.margin,
            seed=args.seed,
            instruction=args.instruction,
            blinded=not args.unblinded,
            emulated=args.emulate,
            workdir=workdir / "site",
            prepare=prepare,
            score=score,
            progress=lambda line: print(line, file=sys.stderr, flush=True),
        )
        report = asyncio.run(Episodes(config).run())
    write_report(report, args.out)
    verdict = report["verdict"]
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["non_inferior"] else 1
