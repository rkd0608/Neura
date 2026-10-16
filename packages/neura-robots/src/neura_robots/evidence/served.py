"""Episodes served by Neura: the simulated ALOHA behind the robot runtime and ``neura-arm``'s
process, a site gateway with its policy worker processes (``qualify.harness.LocalSite``), and
gRPC over TCP through the fault proxy between them.

Each episode resets the scene with its seed, arms the robot as an operator would, lets it run
for at most ``max_steps`` control periods of world time (the physics advances one period per
control tick, whatever the robot is doing), and ends the task at a boundary. A fault ends the
episode as a failure (its reason is recorded) and is reset before the next one.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neura_robots.aloha import AlohaSim
from neura_robots.aloha.arm import AlohaSimArm
from neura_robots.aloha.contracts import Deployment
from neura_robots.aloha.world import LockstepWorld, RealTimeWorld
from neura_robots.evidence.results import EpisodeResult
from neura_robots.process import ArmProcess
from neura_robots.qualify.harness import REVISION, ROBOT_ID, TOKEN, LocalSite
from neura_runtime.runtime import ACTIVE_STATES, RuntimeState
from neura_sim.chaos import FaultProxy

# A fault injected during an episode: a coroutine started when the robot is armed.
Fault = Callable[["ServedRun", FaultProxy], Coroutine[Any, Any, None]]


@dataclass(slots=True)
class _Episode:
    seed: int
    accepted_ages_ms: list[float] = field(default_factory=list)
    faults: list[str] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    max_steps: int = 400


class ServedRun:
    """One site and one robot, serving several episodes."""

    def __init__(
        self,
        deployment: Deployment,
        *,
        task: str = "transfer_cube",
        device: str = "cpu",
        workdir: Path,
        progress: Callable[[str], None] = lambda _: None,
        system: str = "neura",
        realtime: bool = True,
    ) -> None:
        self.deployment = deployment
        self.task = task
        self.device = device
        self.workdir = workdir
        self.progress = progress
        self.system = system
        self.sim = AlohaSim(task)  # type: ignore[arg-type]
        self.world: RealTimeWorld | LockstepWorld = (
            RealTimeWorld(self.sim) if realtime else LockstepWorld(self.sim)
        )
        self.arm = AlohaSimArm(self.world, deployment.binding.robot)
        self.process: ArmProcess | None = None
        self.site: LocalSite | None = None
        self._episode: _Episode | None = None

    def _on_tick(self, now_us: int) -> None:
        self.arm.advance(now_us)
        episode = self._episode
        if episode is None or episode.done.is_set():
            return
        assert self.process is not None
        runtime = self.process.runtime
        if runtime.state is RuntimeState.FAULTED and runtime.fault is not None:
            reason = runtime.fault.reason.value
            if not episode.faults or episode.faults[-1] != reason:
                episode.faults.append(reason)
            episode.done.set()
        elif self.world.done:
            episode.done.set()

    def _watch_chunks(self, process: ArmProcess) -> None:
        """Record the observation age of every chunk the runtime accepts, on arrival: the same
        measure as for LeRobot's async client (``lerobot_async``)."""
        runtime = process.runtime
        accept = runtime.on_chunk

        def watched(chunk: Any, now_us: int) -> bool:
            pending = runtime._pending
            accepted = accept(chunk, now_us)
            if accepted and pending is not None and self._episode is not None:
                self._episode.accepted_ages_ms.append((now_us - pending.capture_time_us) / 1000)
            return accepted

        runtime.on_chunk = watched  # type: ignore[method-assign]

    async def _until(self, condition: Callable[[], bool], timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for {what}")
            await asyncio.sleep(0.01)

    async def episodes(
        self,
        seeds: list[int],
        *,
        max_steps: int = 400,
        fault: Fault | None = None,
        start_timeout_s: float = 600.0,
    ) -> list[EpisodeResult]:
        binding = self.deployment
        results: list[EpisodeResult] = []
        async with LocalSite(
            binding.artifact,
            binding.robot_profile,
            binding.execution_profile,
            binding.checkpoint,
            self.device,
            self.workdir,
            start_timeout_s=start_timeout_s,
        ) as site:
            self.site = site
            if isinstance(self.world, RealTimeWorld):
                self.world.start()
            self.arm.connect()
            process = ArmProcess(
                self.arm,
                binding.binding,
                ROBOT_ID,
                REVISION,
                site.target,
                TOKEN,
                say=lambda line: None,
            )
            process.tick_hooks.append(self._on_tick)
            self.process = process
            self._watch_chunks(process)
            stop = asyncio.Event()
            loop = asyncio.create_task(process.connection.run(stop))
            try:
                runtime = process.runtime
                await self._until(
                    lambda: runtime.session_id is not None, start_timeout_s, "a session"
                )
                if not await site.workers_ready(start_timeout_s):
                    raise RuntimeError(f"the site is not serving; see {site.gateway.log_path}")
                # One unscored warm-up episode: the first requests also pay for lazy
                # initialization, here as in the in-process baselines.
                warm = await self._episode_run(
                    seeds[0] - 1, max_steps, None, site.proxy, warmup=True
                )
                if not warm.counters.get("chunks_accepted"):
                    raise RuntimeError(
                        f"the warm-up episode was served no chunk; see {site.gateway.log_path}"
                    )
                for seed in seeds:
                    results.append(await self._episode_run(seed, max_steps, fault, site.proxy))
            finally:
                stop.set()
                with contextlib.suppress(Exception):
                    await loop
                self.world.close()
        return results

    async def _episode_run(
        self,
        seed: int,
        max_steps: int,
        fault: Fault | None,
        proxy: FaultProxy,
        *,
        warmup: bool = False,
    ) -> EpisodeResult:
        assert self.process is not None
        process, runtime = self.process, self.process.runtime
        if runtime.state is RuntimeState.FAULTED:
            process.inbox.submit("reset")
        await self._until(lambda: runtime.state is RuntimeState.READY, 30, "Ready")
        await self._until(lambda: runtime.session_id is not None, 60, "a session")
        assert self.site is not None
        if not await self.site.workers_ready(180):  # a killed worker is restarted by the gateway
            raise RuntimeError(
                f"the policy workers are not ready; see {self.site.gateway.log_path}"
            )
        before = dict(runtime.counters)
        late_before = process.connection.late_ticks
        self.world.reset(seed, max_steps)
        self.arm.recover()
        episode = _Episode(seed, max_steps=max_steps)
        self._episode = episode
        if isinstance(self.world, RealTimeWorld):
            self.world.go()  # world time starts now; the robot arms at its next tick
        process.inbox.submit("arm")
        fault_task: asyncio.Task[None] | None = (
            asyncio.create_task(fault(self, proxy)) if fault is not None else None
        )
        # World time is bounded by max_steps; the wall clock only guards against a stall.
        await asyncio.wait_for(episode.done.wait(), timeout=max_steps * 0.02 * 10 + 60)
        if fault_task is not None:
            fault_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await fault_task
        proxy.delay_s = 0.0  # the fault is over (a partition ends on its own)
        self._episode = None
        if isinstance(self.world, RealTimeWorld):
            self.world.stop()
        if runtime.state in ACTIVE_STATES:
            process.inbox.submit("stop")
            await self._until(lambda: runtime.state not in ACTIVE_STATES, 10, "the task to end")
        counters = {k: v - before.get(k, 0) for k, v in runtime.counters.items()}
        counters["late_ticks"] = process.connection.late_ticks - late_before
        if isinstance(self.world, RealTimeWorld):
            counters["world_late_steps"] = self.world.late_steps
        counters["max_observation_age_at_acceptance_ms"] = round(
            max(episode.accepted_ages_ms, default=0)
        )
        result = EpisodeResult(
            system=self.system,
            seed=seed,
            success=self.sim.success,
            max_reward=self.sim.max_reward_seen,
            steps=self.world.steps,
            counters={k: int(v) for k, v in counters.items() if v},
            faults=episode.faults,
        )
        label = " (warm-up, not scored)" if warmup else ""
        outcome = "success" if result.success else "fail"
        faults = f", {', '.join(result.faults)}" if result.faults else ""
        self.progress(f"{self.system} seed {seed}{label}: {outcome} ({result.steps} steps{faults})")
        return result
