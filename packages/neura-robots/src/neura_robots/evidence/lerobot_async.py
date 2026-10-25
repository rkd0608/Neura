"""LeRobot's own remote inference (``lerobot.async_inference``, LeRobot 0.6.1) on the simulated
ALOHA: its ``PolicyServer`` in a separate process and its ``RobotClient`` driving the robot, both
unmodified, on the same real-time world (``aloha.world.RealTimeWorld``) and behind the same TCP
fault proxy Neura is measured with.

The only substitutions: the robot is the simulated ALOHA (``AlohaSimRobot``, a LeRobot ``Robot``),
handed to the client in place of a real one; and, when asked (``similarity_atol``), the server's
joint-space similarity tolerance is scaled to the robot's units. The server skips an observation
whose joints are within ``atol=1`` of the last one it processed (unless the client's queue is
empty): one degree on the SO-100 it was written for, but one radian on ALOHA, which would make it
skip nearly everything. Results say which was used.

The robot's camera is read as LeRobot reads a real one (``_Camera``): frames are drawn on a
background thread, and a read returns the newest. LeRobot's client reads the camera every control
tick while it waits for a chunk; drawing each frame in the read instead would cost the client a
software renderer's 40 to 80 ms per tick, slowing its 50 Hz loop by a factor of two or more, which
no real camera does.

Per episode, while the world is stopped: the client is created and started afresh, and the server
loads the policy; then one warm-up inference, through LeRobot's own calls, since a policy's first
forward pass after loading is several times slower than the rest (Neura's workers and the
in-process baseline are warm when an episode starts). Then the client runs until the world's
episode ends. Each received chunk's observation age on arrival is recorded, and each executed
action's: LeRobot does not bound either.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_robots.aloha import JOINTS
from neura_robots.aloha.world import RealTimeWorld
from neura_robots.evidence.results import EpisodeResult
from neura_sim.chaos import FaultProxy, free_port

# A chunk "arrived late" when its observation is older than this on arrival: Neura's acceptance
# budget in the evidence profile, which Neura refuses beyond and LeRobot does not bound.
STALE_AFTER_S = 1.5

# A camera read waits for a new frame when the newest is older than this: the most LeRobot's own
# cameras wait for one (``async_read``'s default timeout).
FRAME_MAX_AGE_S = 0.2


class _Camera:
    """The simulated robot's camera, with the semantics of LeRobot's cameras' ``async_read``: a
    background thread draws frames, and a read returns the newest without waiting for the
    renderer, unless the newest is older than ``max_age_s`` (or none was drawn since
    ``invalidate``): then it waits for one drawn after it asked. Each read asks for the next
    frame, so the thread draws back to back while the robot reads every tick, and not at all
    while it does not read."""

    def __init__(
        self, world: RealTimeWorld, camera: str = "top", max_age_s: float = FRAME_MAX_AGE_S
    ) -> None:
        self.world = world
        self.camera = camera
        self.max_age_s = max_age_s
        self.ages_s: list[float] = []  # each read frame's age when read
        self.waits = 0  # reads that waited for the renderer
        self._changed = threading.Condition()
        self._frame: NDArray[np.uint8] | None = None
        self._drawn_at = 0.0  # when the state the frame shows was copied (monotonic)
        self._epoch = 0
        self._wanted = False
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="aloha-camera", daemon=True)

    def start(self) -> _Camera:
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            while True:
                with self._changed:
                    while not (self._wanted or self._stopping):
                        self._changed.wait()
                    if self._stopping:
                        return
                    self._wanted = False
                    epoch = self._epoch
                drawn_at = time.monotonic()
                image = self.world.render(self.camera)
                with self._changed:
                    if epoch == self._epoch:  # not drawn before an invalidate
                        self._frame, self._drawn_at = image, drawn_at
                    self._changed.notify_all()
        finally:
            self.world.sim.release_renderer()

    def read(self) -> NDArray[np.uint8]:
        with self._changed:
            asked = time.monotonic()
            self._wanted = True
            self._changed.notify_all()
            if self._frame is None or asked - self._drawn_at > self.max_age_s:
                self.waits += 1
                while self._frame is None or self._drawn_at < asked:
                    if self._stopping:
                        raise RuntimeError("the camera is stopped")
                    self._changed.wait()
            self.ages_s.append(time.monotonic() - self._drawn_at)
            return self._frame

    def invalidate(self) -> None:
        """A new episode: no frame drawn before now is read, and the counts start over."""
        with self._changed:
            self._epoch += 1
            self._frame = None
            self.ages_s = []
            self.waits = 0

    def stop(self) -> None:
        with self._changed:
            self._stopping = True
            self._changed.notify_all()
        self._thread.join(timeout=5)


@functools.cache
def _robot_classes() -> tuple[type, type]:
    """The LeRobot robot and its config, defined once, lazily (LeRobot is optional)."""
    from lerobot.robots import Robot, RobotConfig

    @RobotConfig.register_subclass("neura_aloha_sim")
    @dataclass
    class AlohaSimRobotConfig(RobotConfig):  # type: ignore[misc]
        pass

    class AlohaSimRobot(Robot):  # type: ignore[misc]
        config_class = AlohaSimRobotConfig
        name = "neura_aloha_sim"

        def __init__(self, config: Any, world: RealTimeWorld, camera: _Camera) -> None:
            super().__init__(config)
            self.world = world
            self.camera = camera
            self.connected = False
            self.counters: Counter[str] = Counter()

        @property
        def observation_features(self) -> dict[str, Any]:
            features: dict[str, Any] = dict.fromkeys(JOINTS, float)
            features["top"] = (self.world.sim.height, self.world.sim.width, 3)
            return features

        @property
        def action_features(self) -> dict[str, Any]:
            return dict.fromkeys(JOINTS, float)

        @property
        def is_connected(self) -> bool:
            return self.connected

        def connect(self, calibrate: bool = True) -> None:
            self.connected = True

        @property
        def is_calibrated(self) -> bool:
            return True

        def calibrate(self) -> None:
            pass

        def configure(self) -> None:
            pass

        def get_observation(self) -> dict[str, Any]:
            observation: dict[str, Any] = dict(zip(JOINTS, self.world.qpos().tolist(), strict=True))
            observation["top"] = self.camera.read()
            self.counters["observations"] += 1
            return observation

        def send_action(self, action: dict[str, Any]) -> dict[str, Any]:
            self.world.set_targets(np.array([float(action[j]) for j in JOINTS]))
            self.counters["actions"] += 1
            return action

        def disconnect(self) -> None:
            self.connected = False

    return AlohaSimRobot, AlohaSimRobotConfig


# --- the policy server ---------------------------------------------------------------------------


def serve(port: int, similarity_atol: float | None) -> None:
    """LeRobot's PolicyServer, started as its ``serve()`` does."""
    from concurrent import futures

    import grpc
    from lerobot.async_inference import policy_server
    from lerobot.async_inference.configs import PolicyServerConfig
    from lerobot.transport import services_pb2_grpc

    if similarity_atol is not None:
        policy_server.observations_similar = functools.partial(
            policy_server.observations_similar, atol=similarity_atol
        )
    config = PolicyServerConfig(host="127.0.0.1", port=port, fps=50, inference_latency=0.0)
    server_instance = policy_server.PolicyServer(config)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    services_pb2_grpc.add_AsyncInferenceServicer_to_server(server_instance, server)
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    server.wait_for_termination()


class PolicyServerProcess:
    def __init__(self, port: int, similarity_atol: float | None, log: Path) -> None:
        self.port = port
        self.similarity_atol = similarity_atol
        self.log = log
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        command = [
            sys.executable,
            "-m",
            "neura_robots.evidence.lerobot_async",
            "--port",
            str(self.port),
        ]
        if self.similarity_atol is not None:
            command += ["--similarity-atol", repr(self.similarity_atol)]
        # LeRobot leaves a file in TMPDIR for every policy it loads: give it the directory its
        # log is in, which whoever runs this removes.
        scratch = self.log.with_suffix(".tmp")
        scratch.mkdir(exist_ok=True)
        with self.log.open("ab") as log:
            self.process = subprocess.Popen(  # noqa: S603 - our own module
                command,
                stdout=log,
                stderr=log,
                start_new_session=True,
                env={**os.environ, "TMPDIR": str(scratch)},
            )

    def wait_listening(self, timeout_s: float = 180.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"the policy server exited; see {self.log}")
            with (
                contextlib.suppress(OSError),
                socket.create_connection(("127.0.0.1", self.port), timeout=1),
            ):
                return
            time.sleep(0.25)
        raise TimeoutError(f"the policy server is not listening; see {self.log}")

    def kill(self) -> None:
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.kill()


class _ProxyThread:
    """The fault proxy on its own event loop, for LeRobot's synchronous gRPC client."""

    def __init__(self, target_port: int) -> None:
        self.port = free_port()
        self.loop = asyncio.new_event_loop()
        self.proxy = FaultProxy(self.port, target_port)
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.proxy.start(), self.loop).result(timeout=5)

    def call(self, function: Callable[[], None]) -> None:
        self.loop.call_soon_threadsafe(function)

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self.proxy.stop(), self.loop).result(timeout=5)
        asyncio.run_coroutine_threadsafe(self._cancel_the_rest(), self.loop).result(timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        if not self.thread.is_alive():
            self.loop.close()

    @staticmethod
    async def _cancel_the_rest() -> None:
        """The connections' tasks, ended on the loop before it closes (a pump cancelled on a
        closed loop raises)."""
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# --- episodes ------------------------------------------------------------------------------------

AsyncFault = Callable[["AsyncRun"], None]


class AsyncRun:
    def __init__(
        self,
        checkpoint: Path,
        world: RealTimeWorld,
        *,
        workdir: Path,
        actions_per_chunk: int = 100,
        chunk_size_threshold: float = 0.3,
        aggregate_fn_name: str = "latest_only",
        similarity_atol: float | None = None,
        progress: Callable[[str], None] = lambda _: None,
    ) -> None:
        self.checkpoint = checkpoint
        self.world = world
        self.workdir = workdir
        self.actions_per_chunk = actions_per_chunk
        self.chunk_size_threshold = chunk_size_threshold
        self.aggregate_fn_name = aggregate_fn_name
        self.similarity_atol = similarity_atol
        self.progress = progress
        self.system = "lerobot-async" + ("-scaled" if similarity_atol is not None else "")
        workdir.mkdir(parents=True, exist_ok=True)
        self.server = PolicyServerProcess(
            free_port(), similarity_atol, workdir / "policy_server.log"
        )
        self.proxy = _ProxyThread(self.server.port)
        self.camera = _Camera(world)

    def __enter__(self) -> AsyncRun:
        self.server.start()
        self.server.wait_listening()
        self.proxy.start()
        self.camera.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.camera.stop()
        self.proxy.stop()
        self.server.stop()

    def _client(self) -> tuple[Any, Any]:
        from lerobot.async_inference import robot_client
        from lerobot.async_inference.configs import RobotClientConfig

        robot_class, config_class = _robot_classes()
        robot = robot_class(config_class(id="aloha"), self.world, self.camera)
        config = RobotClientConfig(
            policy_type="act",
            pretrained_name_or_path=str(self.checkpoint),
            robot=config_class(id="aloha"),
            actions_per_chunk=self.actions_per_chunk,
            server_address=f"127.0.0.1:{self.proxy.port}",
            policy_device="cpu",
            chunk_size_threshold=self.chunk_size_threshold,
            fps=50,
            aggregate_fn_name=self.aggregate_fn_name,
        )
        original = robot_client.make_robot_from_config
        robot_client.make_robot_from_config = lambda _: robot  # the simulated robot, not a real one
        try:
            client = robot_client.RobotClient(config)
        finally:
            robot_client.make_robot_from_config = original
        return client, robot

    def _warm(self, client: Any) -> None:
        """One inference on the episode's first observation, through LeRobot's own calls, its
        chunk dropped; then ``Ready``, which resets the server's state for a client (the
        observations it has seen and predicted for) but keeps the policy."""
        from lerobot.async_inference.helpers import TimedObservation
        from lerobot.transport import services_pb2

        observation = client.robot.get_observation()
        observation["task"] = ""
        client.send_observation(
            TimedObservation(
                timestamp=time.time(), timestep=0, observation=observation, must_go=True
            )
        )
        if not client.stub.GetActions(services_pb2.Empty()).data:
            raise RuntimeError(f"the policy server did not warm up; see {self.server.log}")
        client.stub.Ready(services_pb2.Empty())

    def episode(
        self,
        seed: int,
        *,
        max_steps: int = 400,
        fault: AsyncFault | None = None,
        warmup: bool = False,
    ) -> EpisodeResult:
        self.server.wait_listening()  # a restarted server takes a moment
        self.world.reset(seed, max_steps)
        self.camera.invalidate()
        client, robot = self._client()
        arrivals: list[float] = []  # observation age when its chunk arrived, seconds
        late_executed = 0  # actions executed from chunks that arrived later than the budget
        # The arrival age of the chunk behind each timestep's queued action (the newest chunk
        # covering a timestep is the one its queued action comes from).
        arrival_age: dict[int, float] = {}
        aggregate = client._aggregate_action_queues

        def recording_aggregate(incoming: list[Any], aggregate_fn: Any = None) -> None:
            if incoming:
                # A chunk's first action carries its observation's timestamp.
                age = time.time() - incoming[0].get_timestamp()
                arrivals.append(age)
                for action in incoming:
                    arrival_age[action.get_timestep()] = age
            aggregate(incoming, aggregate_fn)

        perform = client.control_loop_action

        def recording_action(verbose: bool = False) -> Any:
            nonlocal late_executed
            with client.action_queue_lock:
                head = client.action_queue.queue[0] if client.action_queue.qsize() else None
            if head is not None and arrival_age.get(head.get_timestep(), 0.0) > STALE_AFTER_S:
                late_executed += 1
            return perform(verbose)

        ticks = starved = 0  # control loop ticks; ticks with no action queued, after the first
        available = client.actions_available

        def recording_available() -> bool:  # called once at the top of each tick
            nonlocal ticks, starved
            ticks += 1
            queued: bool = available()
            if not queued and arrivals:
                starved += 1
            return queued

        client._aggregate_action_queues = recording_aggregate
        client.control_loop_action = recording_action
        client.actions_available = recording_available
        if not client.start():
            raise RuntimeError(f"the policy server did not answer; see {self.server.log}")
        self._warm(client)
        robot.counters.clear()
        self.camera.invalidate()  # the episode's first read draws its frame, as Neura's does
        log_mark = self.server.log.stat().st_size
        receiver = threading.Thread(target=client.receive_actions, daemon=True)
        loop = threading.Thread(target=client.control_loop, kwargs={"task": ""}, daemon=True)
        receiver.start()
        loop.start()
        self.world.go()
        if fault is not None:
            threading.Thread(target=fault, args=(self,), daemon=True).start()
        self.world.finished.wait(timeout=max_steps * 0.02 * 10 + 60)
        self.world.stop()
        client.shutdown_event.set()
        loop.join(timeout=10)
        receiver.join(timeout=10)
        with contextlib.suppress(Exception):
            client.channel.close()
        self.proxy.call(self.proxy.proxy.reset)
        self.proxy.proxy.delay_s = 0.0
        server_ms = self._server_times(log_mark)
        result = EpisodeResult(
            system=self.system,
            seed=seed,
            success=self.world.sim.success,
            max_reward=self.world.sim.max_reward_seen,
            steps=self.world.steps,
            counters={
                "chunks_received": len(arrivals),
                "actions_executed": robot.counters["actions"],
                "observations_sent": robot.counters["observations"],
                "actions_from_late_chunks": late_executed,
                "late_chunks": sum(age > STALE_AFTER_S for age in arrivals),
                "max_observation_age_at_arrival_ms": round(max(arrivals, default=0) * 1000),
                "median_observation_age_at_arrival_ms": (
                    round(statistics.median(arrivals) * 1000) if arrivals else 0
                ),
                "world_late_steps": self.world.late_steps,
                "camera_waits": self.camera.waits,
                "max_frame_age_ms": round(max(self.camera.ages_s, default=0) * 1000),
                "control_ticks": ticks,
                "starved_ticks": starved,
                "server_chunks": len(server_ms),
                "server_chunk_ms_median": round(statistics.median(server_ms)) if server_ms else 0,
                "server_chunk_ms_max": round(max(server_ms, default=0)),
            },
        )
        label = " (warm-up, not scored)" if warmup else ""
        outcome = "success" if result.success else "fail"
        c = result.counters
        self.progress(
            f"{self.system} seed {seed}{label}: {outcome} ({result.steps} steps; "
            f"{c['actions_executed']} actions executed in {c['control_ticks']} control ticks, "
            f"{c['starved_ticks']} with none queued; {c['observations_sent']} observations sent, "
            f"{c['chunks_received']} chunks back; server {c['server_chunk_ms_median']} ms a chunk, "
            f"{c['server_chunk_ms_max']} at most; max reward {result.max_reward})"
        )
        return result

    def _server_times(self, since: int) -> list[float]:
        """The server's time for each chunk since the log was ``since`` bytes long, as it logs it
        (its observation's preparation, the policy's pre-processing, forward pass and
        post-processing)."""
        with self.server.log.open("rb") as log:
            log.seek(since)
            text = log.read().decode(errors="replace")
        return [float(ms) for ms in re.findall(r"Observation \d+ \| Total time: ([\d.]+)ms", text)]

    def episodes(
        self, seeds: list[int], *, max_steps: int = 400, fault: AsyncFault | None = None
    ) -> list[EpisodeResult]:
        self.episode(seeds[0] - 1, max_steps=max_steps, warmup=True)
        return [self.episode(s, max_steps=max_steps, fault=fault) for s in seeds]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LeRobot's PolicyServer, for neura-evidence")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--similarity-atol", type=float, default=None)
    args = parser.parse_args(argv)
    serve(args.port, args.similarity_atol)
    return 0


if __name__ == "__main__":
    sys.exit(main())
