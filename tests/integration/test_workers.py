"""The gateway with real policy worker processes (design §9.1, §12): robots served over gRPC by a
worker on a Unix socket, and what robots see when a worker crashes, hangs, fails or cannot load.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from neura_runtime.runtime import RuntimeState
from neura_server import worker
from neura_server.config import (
    DeploymentConfig,
    DeviceConfig,
    GatewayConfig,
    ServingConfig,
    token_digest,
)
from neura_server.grpc_server import GatewayServer
from neura_sim.grpc_robots import SimulatedRobotProcess, load_binding

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "contracts"
ARTIFACT = EXAMPLES / "act-so101.artifact.yaml"
ROBOT = EXAMPLES / "so101.robot.yaml"
PROFILE = EXAMPLES / "act-so101-30hz.profile.yaml"
FAULTY = [sys.executable, str(Path(__file__).with_name("faulty_worker.py"))]


def config(
    robots: list[str],
    *,
    adapter: str | None = "mock",
    command: list[str] | None = None,
    worker_timeout_ms: float = 1000,
) -> GatewayConfig:
    serving: dict[str, Any] = {"backend": "workers", "worker_timeout_ms": worker_timeout_ms}
    if command is not None:
        serving["worker_command"] = command
    return GatewayConfig(
        listen="127.0.0.1:0",
        status_listen=None,
        devices=[DeviceConfig(robot_id=r, token_sha256=token_digest(f"token-{r}")) for r in robots],
        deployments=[
            DeploymentConfig(
                revision_id="rev_test",
                artifact=ARTIFACT,
                robot_profile=ROBOT,
                execution_profile=PROFILE,
                robots=robots,
                adapter=adapter,
                device="cpu",
            )
        ],
        serving=ServingConfig(**serving),
    )


class Site:
    def __init__(self, server: GatewayServer, robots: list[SimulatedRobotProcess]) -> None:
        self.server = server
        self.robots = robots

    def group(self) -> dict[str, Any]:
        group: dict[str, Any] = next(iter(self.server.status()["groups"].values()))
        return group

    def counter(self, code: str) -> int:
        return int(self.server.status()["counters"].get(code, 0))

    def chunks(self) -> int:
        return sum(int(s["chunks"]) for s in self.server.status()["sessions"])

    def reset_faulted_robots(self) -> None:
        """A robot whose queue ran dry while the worker was down stopped itself (design §7);
        the site operator resets it, as on a real robot."""
        for robot in self.robots:
            if robot.runtime.state is RuntimeState.FAULTED:
                assert robot.runtime.fault is not None
                assert robot.runtime.fault.reason.value == "STARVATION_GRACE_EXHAUSTED"
                robot.runtime.reset(time.monotonic_ns() // 1000, revalidated=True)


@contextlib.asynccontextmanager
async def running(cfg: GatewayConfig) -> AsyncIterator[Site]:
    server = GatewayServer(cfg)
    await server.start()
    target = f"127.0.0.1:{server.bound_port}"
    binding = load_binding(ARTIFACT, ROBOT, PROFILE)
    names = [d.robot_id for d in cfg.devices]
    robots = [SimulatedRobotProcess(binding, n, "rev_test", target, f"token-{n}") for n in names]
    stop = asyncio.Event()
    tasks = [asyncio.create_task(r.connection.run(stop)) for r in robots]
    try:
        yield Site(server, robots)
    finally:
        stop.set()
        await asyncio.gather(*tasks)
        await server.stop()


async def until(what: str, check: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


async def test_robots_are_served_by_a_worker_process() -> None:
    async with running(config(["robot-0", "robot-1"])) as site:
        await until("the worker", lambda: site.group()["worker"]["state"] == "ready")
        await until("chunks", lambda: site.chunks() >= 6)
        await until(
            "both robots running",
            lambda: all(r.runtime.state is RuntimeState.RUNNING for r in site.robots),
        )
        worker_status = site.group()["worker"]
    assert worker_status["pid"] not in (None, os.getpid())
    assert worker_status["worker"]["adapter"] == "mock"
    assert worker_status["device"] == "cpu"
    assert all(r.runtime.fault is None for r in site.robots)


async def test_a_crashed_worker_fails_its_batch_and_is_restarted(tmp_path: Path) -> None:
    fault = tmp_path / "fault"
    os.environ["NEURA_TEST_WORKER_FAULT"] = str(fault)
    async with running(config(["robot-0"], adapter="faulty", command=FAULTY)) as site:
        await until("chunks", lambda: site.chunks() >= 2)
        first_pid = site.group()["worker"]["pid"]
        fault.write_text("exit")
        await until("a restart", lambda: site.group()["worker"]["restarts"] == 1)
        await until("the worker again", lambda: site.group()["worker"]["state"] == "ready")
        site.reset_faulted_robots()
        before = site.chunks()
        await until("chunks after the restart", lambda: site.chunks() > before)
        status = site.group()["worker"]
    assert status["pid"] != first_pid
    # The batch in flight when the worker died never got a result from any worker.
    assert site.counter("WORKER_CRASHED") + site.counter("WORKER_UNAVAILABLE") >= 1


async def test_a_hung_worker_is_killed_and_its_batch_fails(tmp_path: Path) -> None:
    fault = tmp_path / "fault"
    os.environ["NEURA_TEST_WORKER_FAULT"] = str(fault)
    cfg = config(["robot-0"], adapter="faulty", command=FAULTY, worker_timeout_ms=300)
    async with running(cfg) as site:
        await until("chunks", lambda: site.chunks() >= 2)
        fault.write_text("hang")
        await until("a timeout", lambda: site.counter("WORKER_TIMED_OUT") >= 1, timeout=10)
        fault.write_text("")
        await until("a restart", lambda: site.group()["worker"]["restarts"] >= 1)
        await until("the worker again", lambda: site.group()["worker"]["state"] == "ready")
        site.reset_faulted_robots()
        before = site.chunks()
        await until("chunks after recovery", lambda: site.chunks() > before)


async def test_a_policy_error_is_a_typed_rejection(tmp_path: Path) -> None:
    fault = tmp_path / "fault"
    os.environ["NEURA_TEST_WORKER_FAULT"] = str(fault)
    async with running(config(["robot-0"], adapter="faulty", command=FAULTY)) as site:
        await until("chunks", lambda: site.chunks() >= 1)
        fault.write_text("error")
        await until("a failure", lambda: site.counter("WORKER_INFERENCE_FAILED") >= 1)
        fault.write_text("")
        assert site.group()["worker"]["restarts"] == 0  # a policy error is not a crash


async def test_a_worker_that_cannot_load_is_reported_and_requests_are_refused() -> None:
    # The artifact names lerobot-act, and no checkpoint is configured.
    async with running(config(["robot-0"], adapter=None)) as site:
        await until("the failure", lambda: site.group()["worker"]["state"] == "failed")
        status = site.group()["worker"]
        await until("a refusal", lambda: site.counter("WORKER_UNAVAILABLE") >= 1)
    assert status["last_error"]["code"] == "AGENT_ARTIFACT_UNAVAILABLE"
    assert site.chunks() == 0


async def test_reloading_without_the_deployment_stops_its_worker() -> None:
    cfg = config(["robot-0"])
    async with running(cfg) as site:
        await until("the worker", lambda: site.group()["worker"]["state"] == "ready")
        pid = site.group()["worker"]["pid"]
        site.server.reload(cfg.model_copy(update={"deployments": []}))
        await until("the group to go", lambda: not site.server.status()["groups"])
        await until("the process to exit", lambda: not _alive(pid))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with contextlib.suppress(ChildProcessError):
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    return True


def test_profile_mode_measures_the_model(capsys: pytest.CaptureFixture[str]) -> None:
    code = worker.main(
        [
            "profile",
            "--artifact",
            str(ARTIFACT),
            "--robot-profile",
            str(ROBOT),
            "--execution-profile",
            str(PROFILE),
            "--adapter",
            "mock",
            "--device",
            "cpu",
            "--batch-sizes",
            "1,4",
            "--iterations",
            "5",
        ]
    )
    assert code == 0
    profile = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["profile"]
    assert [p["batch_size"] for p in profile["points"]] == [1, 4]
    assert all(p["p99_us"] >= p["p50_us"] > 0 for p in profile["points"])
    assert profile["memory_source"] == "process_peak_rss"


def test_load_errors_are_announced(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code = worker.main(
        [
            "serve",
            "--socket",
            str(tmp_path / "w.sock"),
            "--artifact",
            str(ARTIFACT),
            "--robot-profile",
            str(ROBOT),
            "--execution-profile",
            str(PROFILE),
            "--checkpoint",
            str(tmp_path),
            "--device",
            "cpu",
        ]
    )
    assert code == 3
    error = json.loads(capsys.readouterr().out.strip())["error"]
    assert error["code"] == "AGENT_ARTIFACT_UNAVAILABLE"
    assert "model.safetensors is missing" in error["message"]
