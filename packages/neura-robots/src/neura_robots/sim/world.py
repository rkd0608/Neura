"""Time for a simulated robot (any ``scene.Scene``).

``LockstepWorld``: the physics advances one control period each time the robot's control loop
ticks (``advance``). Deterministic; for tests.

``RealTimeWorld``: the physics advances on its own thread every control period of wall-clock
time, whatever the robot software is doing, as the world does: while the policy computes,
while a chunk is late, while the robot holds. The same world for every system served in real
time, so their results compare. Episodes are counted in world steps (``max_steps`` periods).
Rendering copies the state under the lock and draws it outside, so a frame does not stall the
physics.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Generic, TypeVar

import mujoco
import numpy as np
from numpy.typing import NDArray

from neura_robots.sim.scene import Scene

S = TypeVar("S", bound=Scene)


class LockstepWorld(Generic[S]):
    def __init__(self, sim: S) -> None:
        self.sim = sim
        self.max_steps: int | None = None

    def reset(self, seed: int | None, max_steps: int | None = None) -> None:
        self.sim.reset(seed)
        self.max_steps = max_steps

    def advance(self) -> None:
        if self.max_steps is None or self.sim.steps < self.max_steps:
            self.sim.advance()

    def set_targets(self, action: NDArray[np.floating[Any]]) -> None:
        self.sim.set_targets(action)

    def hold(self) -> NDArray[np.float64]:
        present = self.sim.qpos()
        self.sim.set_targets(present)
        return present

    def qpos(self) -> NDArray[np.float64]:
        return self.sim.qpos()

    def targets(self) -> NDArray[np.float64]:
        return self.sim.targets()

    def render(self, camera: str) -> NDArray[np.uint8]:
        return self.sim.render(camera)

    @property
    def steps(self) -> int:
        return self.sim.steps

    @property
    def done(self) -> bool:
        return self.sim.success or (self.max_steps is not None and self.sim.steps >= self.max_steps)

    def close(self) -> None:
        self.sim.close()


class RealTimeWorld(Generic[S]):
    def __init__(self, sim: S, period_s: float | None = None) -> None:
        self.sim = sim
        self.period_s = sim.dt if period_s is None else period_s
        self.max_steps: int | None = None
        self.late_steps = 0
        self._lock = threading.Lock()
        self._shadow = mujoco.MjData(sim.model)
        self._running = False
        self._stepping = False
        self._thread: threading.Thread | None = None
        self.finished = threading.Event()

    # --- the clock -------------------------------------------------------------------------------

    def start(self) -> RealTimeWorld[S]:
        self._running = True
        self._thread = threading.Thread(target=self._run, name="sim-world", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        next_step = time.monotonic()
        while self._running:
            with self._lock:
                if self._stepping and not self._done():
                    self.sim.advance()
                    if self._done():
                        self.finished.set()
            next_step += self.period_s
            delay = next_step - time.monotonic()
            if delay < 0:
                self.late_steps += 1
                next_step = time.monotonic()
            else:
                time.sleep(delay)

    def _done(self) -> bool:
        return self.sim.success or (self.max_steps is not None and self.sim.steps >= self.max_steps)

    def reset(self, seed: int | None, max_steps: int | None = None) -> None:
        """A new episode: the scene from its seed, time stopped until ``go``."""
        with self._lock:
            self._stepping = False
            self.sim.reset(seed)
            self.max_steps = max_steps
            self.late_steps = 0
            self.finished.clear()

    def go(self) -> None:
        with self._lock:
            self._stepping = True

    def stop(self) -> None:
        with self._lock:
            self._stepping = False

    # --- the robot's view ------------------------------------------------------------------------

    def advance(self) -> None:
        """The robot's tick: time is the thread's, not the robot's."""

    def set_targets(self, action: NDArray[np.floating[Any]]) -> None:
        with self._lock:
            self.sim.set_targets(action)

    def hold(self) -> NDArray[np.float64]:
        with self._lock:
            present = self.sim.qpos()
            self.sim.set_targets(present)
            return present

    def qpos(self) -> NDArray[np.float64]:
        with self._lock:
            return self.sim.qpos()

    def targets(self) -> NDArray[np.float64]:
        with self._lock:
            return self.sim.targets()

    def render(self, camera: str) -> NDArray[np.uint8]:
        self.copy_state(self._shadow)
        return self.sim.render(camera, data=self._shadow)

    def copy_state(self, into: mujoco.MjData) -> None:
        """The scene's state now, for a renderer on another thread (an onlooker's camera)."""
        with self._lock:
            mujoco.mj_copyData(into, self.sim.model, self.sim.data)

    @property
    def steps(self) -> int:
        return self.sim.steps

    @property
    def done(self) -> bool:
        with self._lock:
            return self._done()

    def close(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.sim.close()
