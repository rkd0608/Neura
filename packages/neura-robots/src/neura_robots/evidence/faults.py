"""The same faults for every remote system, on the same real-time world and the same TCP fault
proxy between the robot and whatever serves it.

- ``delay_<ms>``: that much latency added each way for the whole episode;
- ``partition``: the network stalls for 3 s, 2 s into the episode (nothing is lost: what was
  sent arrives late, as through a congested or flapping link);
- ``inference_kill``: 2 s in, the process running the model is killed. For Neura the policy
  worker (the site gateway restarts it); for LeRobot its policy server, restarted on the same
  port.

What is measured, per episode: task success; whether the robot faulted (Neura latches a fault
and holds until an operator resets); and the observation age on arrival of the chunks it acted
on, against the 1.5 s budget Neura refuses chunks beyond.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from neura_robots.evidence.lerobot_async import AsyncRun
from neura_robots.evidence.served import ServedRun
from neura_sim.chaos import FaultProxy

FAULT_AT_S = 2.0
PARTITION_S = 3.0


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    delay_ms: int = 0
    partition: bool = False
    kill: bool = False

    def describe(self) -> str:
        if self.delay_ms:
            return f"{self.delay_ms} ms added each way"
        if self.partition:
            return f"network stalled for {PARTITION_S:g} s, {FAULT_AT_S:g} s into the episode"
        if self.kill:
            return f"model process killed {FAULT_AT_S:g} s into the episode"
        return "no fault"


SCENARIOS = {
    s.name: s
    for s in (
        Scenario("nominal"),
        Scenario("delay_50", delay_ms=50),
        Scenario("delay_100", delay_ms=100),
        Scenario("delay_200", delay_ms=200),
        Scenario("delay_400", delay_ms=400),
        Scenario("partition", partition=True),
        Scenario("inference_kill", kill=True),
    )
}


def neura_fault(scenario: Scenario) -> Any:
    """A fault coroutine for ``ServedRun.episodes`` (None for nominal)."""
    if scenario.name == "nominal":
        return None

    async def inject(run: ServedRun, proxy: FaultProxy) -> None:
        if scenario.delay_ms:
            proxy.delay_s = scenario.delay_ms / 1000
            return
        await asyncio.sleep(FAULT_AT_S)
        if scenario.partition:
            proxy.partition(PARTITION_S)
        elif scenario.kill:
            assert run.site is not None
            workers = await asyncio.to_thread(run.site.workers)
            for worker in workers:
                if worker.get("pid"):
                    os.kill(int(worker["pid"]), signal.SIGKILL)

    def start(run: ServedRun, proxy: FaultProxy) -> Coroutine[Any, Any, None]:
        return inject(run, proxy)

    return start


def async_fault(scenario: Scenario) -> Any:
    """A fault function for ``AsyncRun.episodes`` (run on its own thread; None for nominal)."""
    if scenario.name == "nominal":
        return None

    def inject(run: AsyncRun) -> None:
        if scenario.delay_ms:
            run.proxy.proxy.delay_s = scenario.delay_ms / 1000
            return
        time.sleep(FAULT_AT_S)
        if scenario.partition:
            run.proxy.call(lambda: run.proxy.proxy.partition(PARTITION_S))
        elif scenario.kill:
            run.server.kill()
            run.server.start()  # restarted on the same port, as a supervisor would

    return inject
