"""Chaos against real processes: ``neura-chaos``.

Starts a site gateway process, with its real policy worker processes, behind a TCP fault proxy,
and runs simulated robots against it over gRPC, each watched by the independent
``InvariantMonitor``. A supervisor restarts the gateway when it dies, as systemd does on a site
server. For the given duration it then injects faults into the processes and the network:

  kill_worker        SIGKILL a policy worker mid-batch; the gateway restarts it
  hang_worker        SIGSTOP a worker for up to 3 s; the gateway times it out and restarts it
  kill_gateway       SIGKILL the gateway; leases and generations survive in its SQLite store
  terminate_gateway  SIGTERM the gateway (a planned restart)
  freeze_gateway     SIGSTOP the whole gateway for up to 2.5 s (an overloaded or paused host)
  reload_gateway     SIGHUP: reload the configuration under load
  partition          the proxy stalls every connection for up to 4 s; TCP delivers the backlog
                     late when it heals, so robots receive stale replies
  reset_connections  the proxy drops every connection at once
  network_delay      the proxy adds 20-400 ms of latency for a few seconds
  operator_takeover  an operator takes a robot over and hands it back later
  second_client      a second process with the same robot credential runs for a while

An operator stand-in resets a latched fault a few seconds after it happens. After the last fault
the site gets a cooldown; then every robot must be running again, and the monitor must have seen
no violation. The JSON report lists what was injected and what was observed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import signal
import socket
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from neura_runtime.runtime import RuntimeState
from neura_server.config import token_digest
from neura_server.profiles import DEFAULT_HORIZON_US
from neura_sim.grpc_robots import SimulatedRobotProcess, load_binding
from neura_sim.invariants import InvariantMonitor


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _now_us() -> int:
    return time.monotonic_ns() // 1000


# --- the fault proxy -----------------------------------------------------------------------------


class FaultProxy:
    """A TCP proxy between the robots and the gateway that can stall, delay or drop traffic."""

    def __init__(self, listen_port: int, target_port: int) -> None:
        self.listen_port = listen_port
        self.target_port = target_port
        self.delay_s = 0.0
        self._open = asyncio.Event()
        self._open.set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None
        self.connections = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", self.listen_port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        self.reset()

    def partition(self, seconds: float) -> None:
        self._open.clear()
        asyncio.get_running_loop().call_later(seconds, self._open.set)

    def reset(self) -> None:
        for writer in list(self._writers):
            writer.transport.abort()
        self._writers.clear()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        await self._open.wait()
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.target_port
            )
        except OSError:
            writer.transport.abort()  # the gateway is down: connection refused
            return
        self._writers |= {writer, upstream_writer}
        try:
            await asyncio.gather(
                self._pump(reader, upstream_writer), self._pump(upstream_reader, writer)
            )
        finally:
            for w in (writer, upstream_writer):
                self._writers.discard(w)
                w.transport.abort()

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        queue: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue()

        async def send() -> None:
            while True:
                due, data = await queue.get()
                if not data:
                    return
                await self._open.wait()  # a partition stalls delivery; nothing is lost
                wait = due - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                writer.write(data)
                await writer.drain()

        sender = asyncio.create_task(send())
        try:
            while data := await reader.read(65536):
                await queue.put((time.monotonic() + self.delay_s, data))
            await queue.put((0.0, b""))
            await sender
        except (ConnectionError, OSError):
            pass
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionError, OSError):
                await sender
            writer.transport.abort()


# --- the gateway process -------------------------------------------------------------------------


class GatewayProcess:
    """Runs ``python -m neura_server`` and restarts it whenever it exits, like systemd would."""

    def __init__(self, config: Path, log: Path, restart_delay_s: float = 1.0) -> None:
        self.config = config
        self.log_path = log
        self.restart_delay_s = restart_delay_s
        self.process: asyncio.subprocess.Process | None = None
        self.starts = 0
        self.exits: Counter[str] = Counter()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        with self.log_path.open("ab") as log:
            while not self._stopping:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "neura_server",
                    "--config",
                    str(self.config),
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
                self.starts += 1
                code = await self.process.wait()
                self.exits[str(code)] += 1
                if not self._stopping:
                    await asyncio.sleep(self.restart_delay_s)

    async def stop(self) -> None:
        self._stopping = True
        process = self.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGCONT)
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    @property
    def pid(self) -> int | None:
        process = self.process
        return process.pid if process is not None and process.returncode is None else None

    def signal(self, sig: signal.Signals) -> bool:
        pid = self.pid
        if pid is None:
            return False
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)
            return True
        return False

    def workers(self) -> list[int]:
        """Policy worker processes: the gateway's children."""
        pid = self.pid
        if pid is None:
            return []
        children: list[int] = []
        with contextlib.suppress(OSError):
            for task in Path(f"/proc/{pid}/task").iterdir():
                text = (task / "children").read_text()
                children.extend(int(c) for c in text.split())
        return children


# --- the run -------------------------------------------------------------------------------------


@dataclass(slots=True)
class ChaosConfig:
    duration_s: float = 300.0
    robots: int = 3
    seed: int = 1
    mean_fault_interval_s: float = 6.0
    cooldown_s: float = 30.0
    reset_after_s: tuple[float, float] = (1.0, 5.0)
    faults: tuple[str, ...] = ()
    contracts: Path = Path("examples/contracts")
    model: str = "act"
    workdir: Path | None = None
    progress: Callable[[str], None] | None = None


@dataclass(slots=True)
class _Robot:
    process: SimulatedRobotProcess
    stop: asyncio.Event
    task: asyncio.Task[None]
    hold_until: float = 0.0
    reset_at: float | None = None


@dataclass(slots=True)
class _Outcome:
    injected: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    resets: int = 0


class Chaos:
    def __init__(self, config: ChaosConfig) -> None:
        self.config = config
        self.rng = random.Random(config.seed)
        contracts = config.contracts.resolve()
        self.files = (
            contracts / f"{config.model}-so101.artifact.yaml",
            contracts / "so101.robot.yaml",
            contracts / f"{config.model}-so101-30hz.profile.yaml",
        )
        self.binding = load_binding(*self.files)
        self.monitor = InvariantMonitor(clock=_now_us, gateway_observed=False)
        self.names = [f"robot-{i}" for i in range(config.robots)]
        self.robots: list[_Robot] = []
        self.extra: list[_Robot] = []
        self.outcome = _Outcome()
        self.final_status: dict[str, Any] | None = None

    def _log(self, line: str) -> None:
        if self.config.progress is not None:
            self.config.progress(line)

    def _write_config(self, workdir: Path, gateway_port: int, status_port: int) -> Path:
        artifact, robot, profile = self.files
        config = {
            "listen": f"127.0.0.1:{gateway_port}",
            "status_listen": f"127.0.0.1:{status_port}",
            "lease_store": str(workdir / "leases.sqlite"),
            "lease_ttl_ms": 2000,
            "devices": [
                {"robot_id": name, "token_sha256": token_digest(f"chaos-{name}")}
                for name in self.names
            ],
            "deployments": [
                {
                    "revision_id": "rev_chaos",
                    "artifact": str(artifact),
                    "robot_profile": str(robot),
                    "execution_profile": str(profile),
                    "robots": self.names,
                    "adapter": "mock",
                    "device": "cpu",
                }
            ],
            "serving": {
                "backend": "workers",
                "worker_timeout_ms": 1000,
                "worker_dir": str(workdir / "w"),
            },
        }
        path = workdir / "site.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return path

    async def run(self) -> dict[str, Any]:
        config = self.config
        with contextlib.ExitStack() as stack:
            if config.workdir is None:
                # Short on purpose: worker Unix socket paths have a length limit.
                workdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="nc-")))
            else:
                workdir = config.workdir
                workdir.mkdir(parents=True, exist_ok=True)
            gateway_port, status_port, proxy_port = _free_port(), _free_port(), _free_port()
            site_config = self._write_config(workdir, gateway_port, status_port)
            self.status_url = f"http://127.0.0.1:{status_port}"
            gateway = GatewayProcess(site_config, workdir / "gateway.log")
            proxy = FaultProxy(proxy_port, gateway_port)
            self.gateway, self.proxy = gateway, proxy
            await gateway.start()
            await proxy.start()
            started = time.monotonic()
            try:
                await self._wait_ready(timeout_s=120)
                for name in self.names:
                    self.robots.append(self._start_robot(name, f"127.0.0.1:{proxy_port}"))
                await self._wait_running(timeout_s=60)
                self._log(f"site running: {len(self.robots)} robots; injecting faults")
                operator = asyncio.create_task(self._operate())
                await self._inject_until(started + config.duration_s)
                self._log("faults stopped; cooling down")
                proxy.delay_s = 0.0
                await asyncio.sleep(config.cooldown_s)
                # Longer than the latency profile's horizon: after repeated stalls admission
                # may refuse a robot until those observations expire (neura_server.profiles).
                recovered = await self._wait_running(timeout_s=DEFAULT_HORIZON_US / 1e6 + 30)
                self.final_status = await asyncio.to_thread(self._status)
                operator.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await operator
                report = self._report(time.monotonic() - started, recovered)
            finally:
                for robot in [*self.robots, *self.extra]:
                    robot.stop.set()
                await asyncio.gather(
                    *(r.task for r in [*self.robots, *self.extra]), return_exceptions=True
                )
                await proxy.stop()
                await gateway.stop()
            report["gateway_log"] = str(workdir / "gateway.log") if config.workdir else None
            return report

    def _start_robot(self, name: str, target: str) -> _Robot:
        process = SimulatedRobotProcess(
            self.binding,
            name,
            "rev_chaos",
            target,
            f"chaos-{name}",
            task_seconds=self.rng.uniform(10, 40),
        )
        process.watch(self.monitor)
        stop = asyncio.Event()
        return _Robot(process, stop, asyncio.create_task(process.connection.run(stop)))

    def _status(self) -> dict[str, Any] | None:
        try:
            url = f"{self.status_url}/v1/local/dataplane"  # always http://127.0.0.1
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                data: dict[str, Any] = json.loads(response.read())
                return data
        except (OSError, ValueError):
            return None

    async def _wait_ready(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await asyncio.to_thread(self._status) is not None:
                return
            await asyncio.sleep(0.5)
        raise RuntimeError("the gateway did not start; see its log")

    async def _wait_running(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if all(r.process.runtime.state is RuntimeState.RUNNING for r in self.robots):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _operate(self) -> None:
        """Hand robots back after a takeover and reset latched faults after a few seconds."""
        while True:
            now = time.monotonic()
            for robot in self.robots:
                process = robot.process
                process.hold = now < robot.hold_until
                if process.runtime.state is not RuntimeState.FAULTED:
                    robot.reset_at = None
                    continue
                if robot.reset_at is None:
                    robot.reset_at = now + self.rng.uniform(*self.config.reset_after_s)
                elif now >= robot.reset_at:
                    robot.reset_at = None
                    with contextlib.suppress(Exception):
                        process.reset(_now_us())
                        self.outcome.resets += 1
            await asyncio.sleep(0.25)

    async def _inject_until(self, end: float) -> None:
        faults = {
            n: f for n, f in FAULTS.items() if not self.config.faults or n in self.config.faults
        }
        names = sorted(faults)
        next_report = time.monotonic() + 30
        while True:
            gap = self.rng.expovariate(1.0 / self.config.mean_fault_interval_s)
            if time.monotonic() + gap >= end:
                await asyncio.sleep(max(0.0, end - time.monotonic()))
                return
            await asyncio.sleep(gap)
            name = self.rng.choice(names)
            if await faults[name](self):
                self.outcome.injected[name] += 1
            else:
                self.outcome.skipped[name] += 1
            if time.monotonic() >= next_report:
                next_report += 30
                self._log(
                    f"faults={sum(self.outcome.injected.values())} "
                    f"violations={self.monitor.violations} resets={self.outcome.resets} "
                    f"gateway starts={self.gateway.starts} states="
                    + ",".join(r.process.runtime.state.value for r in self.robots)
                )

    def _report(self, wall_s: float, recovered: bool) -> dict[str, Any]:
        period_s = self.binding.control_period_us / 1e6
        robots = {
            r.process.robot_id: {
                "state": r.process.runtime.state.value,
                "dispatched": r.process.runtime.dispatched,
                "availability": round(r.process.runtime.dispatched * period_s / wall_s, 4),
                "resets": r.process.resets,
                "tasks": r.process.tasks_completed,
                "late_ticks": r.process.connection.late_ticks,
                "last_rejection": (
                    r.process.client.last_rejection.value
                    if r.process.client.last_rejection
                    else None
                ),
                "last_reason": (
                    r.process.runtime.last_reason.value if r.process.runtime.last_reason else None
                ),
            }
            for r in self.robots
        }
        counters: Counter[str] = Counter()
        for robot in [*self.robots, *self.extra]:
            counters.update(robot.process.runtime.counters)
        findings = [] if recovered else ["not every robot was running after the cooldown"]
        passed = self.monitor.violations == 0 and recovered
        return {
            "passed": passed,
            "wall_seconds": round(wall_s, 1),
            "seed": self.config.seed,
            "faults_injected": dict(sorted(self.outcome.injected.items())),
            "faults_skipped": dict(sorted(self.outcome.skipped.items())),
            "faults_total": sum(self.outcome.injected.values()),
            "operator_resets": self.outcome.resets,
            "gateway_starts": self.gateway.starts,
            "gateway_exits": dict(self.gateway.exits),
            "proxy_connections": self.proxy.connections,
            "invariants": self.monitor.summary(),
            "findings": findings,
            "robots": robots,
            "runtime_counters": dict(sorted(counters.items())),
            "gateway": self.final_status,
        }


# --- the fault catalog ---------------------------------------------------------------------------


async def _kill_worker(chaos: Chaos) -> bool:
    workers = chaos.gateway.workers()
    if not workers:
        return False
    with contextlib.suppress(ProcessLookupError):
        os.kill(chaos.rng.choice(workers), signal.SIGKILL)
        return True
    return False


async def _hang_worker(chaos: Chaos) -> bool:
    workers = chaos.gateway.workers()
    if not workers:
        return False
    pid = chaos.rng.choice(workers)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGSTOP)
        _later(chaos.rng.uniform(0.3, 3.0), lambda: os.kill(pid, signal.SIGCONT))
        return True
    return False


async def _kill_gateway(chaos: Chaos) -> bool:
    return chaos.gateway.signal(signal.SIGKILL)


async def _terminate_gateway(chaos: Chaos) -> bool:
    return chaos.gateway.signal(signal.SIGTERM)


async def _freeze_gateway(chaos: Chaos) -> bool:
    pid = chaos.gateway.pid
    if pid is None:
        return False
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGSTOP)  # the gateway and its workers: the whole host stalls
        _later(chaos.rng.uniform(0.1, 2.5), lambda: os.killpg(pid, signal.SIGCONT))
        return True
    return False


async def _reload_gateway(chaos: Chaos) -> bool:
    return chaos.gateway.signal(signal.SIGHUP)


async def _partition(chaos: Chaos) -> bool:
    chaos.proxy.partition(chaos.rng.uniform(0.1, 4.0))
    return True


async def _reset_connections(chaos: Chaos) -> bool:
    chaos.proxy.reset()
    return True


async def _network_delay(chaos: Chaos) -> bool:
    proxy = chaos.proxy
    proxy.delay_s = chaos.rng.uniform(0.02, 0.4)
    _later(chaos.rng.uniform(1.0, 8.0), lambda: setattr(proxy, "delay_s", 0.0))
    return True


async def _operator_takeover(chaos: Chaos) -> bool:
    robot = chaos.rng.choice(chaos.robots)
    robot.process.runtime.operator_takeover(_now_us())
    robot.hold_until = time.monotonic() + chaos.rng.uniform(1.0, 10.0)
    robot.process.hold = True
    return True


async def _second_client(chaos: Chaos) -> bool:
    name = chaos.rng.choice(chaos.names)
    target = chaos.robots[0].process.connection.target
    duplicate = chaos._start_robot(name, target)
    chaos.extra.append(duplicate)
    _later(chaos.rng.uniform(2.0, 20.0), duplicate.stop.set)
    return True


def _later(delay_s: float, action: Callable[[], object]) -> None:
    def run() -> None:
        with contextlib.suppress(ProcessLookupError):
            action()

    asyncio.get_running_loop().call_later(delay_s, run)


FAULTS: dict[str, Callable[[Chaos], Awaitable[bool]]] = {
    "kill_worker": _kill_worker,
    "hang_worker": _hang_worker,
    "kill_gateway": _kill_gateway,
    "terminate_gateway": _terminate_gateway,
    "freeze_gateway": _freeze_gateway,
    "reload_gateway": _reload_gateway,
    "partition": _partition,
    "reset_connections": _reset_connections,
    "network_delay": _network_delay,
    "operator_takeover": _operator_takeover,
    "second_client": _second_client,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neura-chaos", description=__doc__.split("\n\n")[0])
    parser.add_argument("--duration", type=float, default=300.0, help="seconds of fault injection")
    parser.add_argument("--robots", type=int, default=3)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--mean-fault-interval", type=float, default=6.0, help="seconds")
    parser.add_argument("--cooldown", type=float, default=30.0, help="seconds after the last fault")
    parser.add_argument("--fault", action="append", default=[], choices=sorted(FAULTS))
    parser.add_argument("--contracts", type=Path, default=Path("examples/contracts"))
    parser.add_argument("--model", default="act", help="example model: act or smolvla")
    parser.add_argument("--workdir", type=Path, help="keep the gateway config, store and log here")
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    args = parser.parse_args(argv)
    config = ChaosConfig(
        duration_s=args.duration,
        robots=args.robots,
        seed=args.seed,
        mean_fault_interval_s=args.mean_fault_interval,
        cooldown_s=args.cooldown,
        faults=tuple(args.fault),
        contracts=args.contracts,
        model=args.model,
        workdir=args.workdir,
        progress=lambda line: print(line, file=sys.stderr, flush=True),
    )
    report = asyncio.run(Chaos(config).run())
    text = json.dumps(report, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
