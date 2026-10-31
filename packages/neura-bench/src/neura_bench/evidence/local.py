"""The in-process baselines: the policy and the scene in one process, no Neura.

``lerobot-eval``: the world waits while the policy computes (the protocol LeRobot publishes its
success rates with), in lockstep: one action, one control period.

``lerobot-realtime``: the policy in the robot's own 50 Hz control loop, as on a robot computer
running ``lerobot-record --policy.path``: when the action queue is empty the loop blocks on
inference, and the world (``RealTimeWorld``, the same clock Neura is measured on) keeps moving
meanwhile, with the arm holding its last goal.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from neura_bench.evidence.policy import LocalPolicy
from neura_bench.evidence.results import EpisodeResult
from neura_robots.aloha import DT, AlohaSim
from neura_robots.sim.world import RealTimeWorld

Progress = Callable[[str], None]


def _order(seeds: list[int], warmup: bool) -> list[tuple[int, bool]]:
    """(seed, scored): one unscored warm-up episode first (seed ``seeds[0] - 1``), since the first
    inference of a process is slower (lazy initialization)."""
    return ([(seeds[0] - 1, False)] if warmup and seeds else []) + [(s, True) for s in seeds]


def _report(progress: Progress, result: EpisodeResult, scored: bool) -> None:
    label = "" if scored else " (warm-up, not scored)"
    outcome = "success" if result.success else "fail"
    progress(f"{result.system} seed {result.seed}{label}: {outcome} ({result.steps} steps)")


def run_eval_protocol(
    policy: LocalPolicy,
    sim: AlohaSim,
    seeds: list[int],
    *,
    max_steps: int = 400,
    warmup: bool = True,
    progress: Progress = lambda _: None,
) -> list[EpisodeResult]:
    results = []
    for seed, scored in _order(seeds, warmup):
        sim.reset(seed)
        policy.reset()
        inference_ms: list[float] = []
        image = None
        while sim.steps < max_steps and not sim.success:
            computes = policy.queue_empty or policy.reads_every_observation
            # The observation matters only when the model runs: render then (the queue pops
            # without reading it otherwise, so the actions are the same).
            if computes or image is None:
                image = sim.render("top")
            started = time.perf_counter()
            action = policy.select_action(image, sim.qpos())
            if computes:
                inference_ms.append((time.perf_counter() - started) * 1000)
            sim.step(action)
        result = EpisodeResult(
            system="lerobot-eval",
            seed=seed,
            success=sim.success,
            max_reward=sim.max_reward_seen,
            steps=sim.steps,
            inference_ms=inference_ms,
        )
        _report(progress, result, scored)
        if scored:
            results.append(result)
    return results


def run_realtime(
    policy: LocalPolicy,
    world: RealTimeWorld[AlohaSim],
    seeds: list[int],
    *,
    max_steps: int = 400,
    warmup: bool = True,
    progress: Progress = lambda _: None,
) -> list[EpisodeResult]:
    results = []
    for seed, scored in _order(seeds, warmup):
        world.reset(seed, max_steps)
        policy.reset()
        inference_ms: list[float] = []
        late_loops = 0
        image = None
        world.go()
        next_loop = time.monotonic()
        while not world.done:
            computes = policy.queue_empty or policy.reads_every_observation
            if computes or image is None:
                image = world.render("top")
            started = time.perf_counter()
            action = policy.select_action(image, world.qpos())  # blocks while the model runs
            if computes:
                inference_ms.append((time.perf_counter() - started) * 1000)
            world.set_targets(action)
            next_loop += DT
            delay = next_loop - time.monotonic()
            if delay < 0:
                late_loops += 1
                next_loop = time.monotonic()
            else:
                time.sleep(delay)
        world.stop()
        result = EpisodeResult(
            system="lerobot-realtime",
            seed=seed,
            success=world.sim.success,
            max_reward=world.sim.max_reward_seen,
            steps=world.steps,
            inference_ms=inference_ms,
            counters={"late_loops": late_loops, "world_late_steps": world.late_steps},
        )
        _report(progress, result, scored)
        if scored:
            results.append(result)
    return results
