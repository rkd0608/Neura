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

Per episode, the client is created and started afresh (the server reloads the policy, while the
world is stopped), then runs until the world's episode ends. Each received chunk's observation
age on arrival is recorded, and each executed action's: LeRobot does not bound either.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import os
import signal
import socket
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

from neura_robots.aloha import JOINTS
from neura_robots.aloha.world import RealTimeWorld
from neura_robots.evidence.results import EpisodeResult
from neura_sim.chaos import FaultProxy, free_port

# A chunk "arrived late" when its observation is older than this on arrival: Neura's acceptance
# budget in the evidence profile, which Neura refuses beyond and LeRobot does not bound.
STALE_AFTER_S = 1.5


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

        def __init__(self, config: Any, world: RealTimeWorld) -> None:
            super().__init__(config)
            self.world = world
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
            observation["top"] = self.world.render("top")
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
        with self.log.open("ab") as log:
            self.process = subprocess.Popen(  # noqa: S603 - our own module
                command, stdout=log, stderr=log, start_new_session=True
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
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


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

    def __enter__(self) -> AsyncRun:
        self.server.start()
        self.server.wait_listening()
        self.proxy.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.proxy.stop()
        self.server.stop()

    def _client(self) -> tuple[Any, Any]:
        from lerobot.async_inference import robot_client
        from lerobot.async_inference.configs import RobotClientConfig

        robot_class, config_class = _robot_classes()
        robot = robot_class(config_class(id="aloha"), self.world)
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

        client._aggregate_action_queues = recording_aggregate
        client.control_loop_action = recording_action
        if not client.start():
            raise RuntimeError(f"the policy server did not answer; see {self.server.log}")
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
                "world_late_steps": self.world.late_steps,
            },
        )
        label = " (warm-up, not scored)" if warmup else ""
        outcome = "success" if result.success else "fail"
        self.progress(f"{self.system} seed {seed}{label}: {outcome} ({result.steps} steps)")
        return result

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
