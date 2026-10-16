"""Hardware qualification: the fault suite and open-loop parity on a real arm (design §7.1).

``Qualification`` runs a site gateway process, with its real policy worker processes, behind the
TCP fault proxy of ``neura-chaos``, on the robot computer (or a machine next to it), and serves
the arm through ``neura-arm``'s own process code, watched by the independent invariant monitor.
Each scenario arms the robot, lets it run, injects one fault, then checks what the robot did:
the runtime's verdict (state, reason, whether the local response was confirmed) and what the arm
physically did, from its joint readings (it held still within tolerance, and received no command
while faulted or starved). Then it resets, as an operator would, and moves to the next.

Parity: during the nominal run the harness keeps the raw readings behind some observations and
the chunks the site returned for them, then runs the checkpoint through LeRobot alone on the same
readings (``reference.LeRobotReference``) and compares.

On hardware the harness acts as the operator: it arms the robot for each scenario, after the
human operator has confirmed they are at the power switch with the workspace clear. The arm moves
under the policy: qualify with a trained checkpoint, never with random weights.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import signal
import tempfile
import threading
import time
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from numpy.typing import NDArray

from neura.v1 import dataplane_pb2
from neura_contracts import ReasonCode
from neura_robots.emulator import SO101Emulator
from neura_robots.lock import ActuatorBusyError
from neura_robots.process import ArmProcess
from neura_robots.so101 import So101Arm, So101Config
from neura_runtime import tensors
from neura_runtime.binding import ExecutionBinding
from neura_runtime.grpc_client import monotonic_us
from neura_runtime.interfaces import CommandResult, SensorSnapshot
from neura_runtime.runtime import RuntimeState, TransitionError
from neura_server.config import token_digest
from neura_sim.chaos import FaultProxy, GatewayProcess, free_port
from neura_sim.grpc_robots import SimulatedRobotProcess, load_binding
from neura_sim.invariants import InvariantMonitor

ROBOT_ID = "qualify-arm"
TOKEN = "qualify-token"  # noqa: S105 - the qualification gateway listens on 127.0.0.1 only
REVISION = "rev_qualify"


@dataclass(slots=True)
class QualifyConfig:
    arm: So101Config
    artifact: Path
    robot_profile: Path
    execution_profile: Path
    # The checkpoint the site serves and the reference runs. None: the artifact's adapter is
    # replaced by the mock, and parity is not measured (the qualification cannot pass).
    checkpoint: Path | None = None
    device: str = "cpu"  # where the site's worker and the reference run
    scenarios: tuple[str, ...] = ()  # empty: every scenario that applies
    manual: bool = False  # include scenarios the operator performs (unplugging the bus)
    nominal_s: float = 20.0
    hold_tolerance: float = 1.0  # degrees (0-100 units for the gripper)
    settle_s: float = 0.3  # after a hold is commanded, before it is measured
    parity_samples: int = 10
    parity_atol: float = 1e-3  # in the artifact's action units
    instruction: str | None = None
    emulator: SO101Emulator | None = None
    workdir: Path | None = None
    start_timeout_s: float = 180.0
    confirm: Callable[[str], bool] = lambda _: True
    progress: Callable[[str], None] = lambda _: None


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(slots=True)
class ScenarioResult:
    name: str
    checks: list[Check] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)
    skipped: str | None = None

    @property
    def passed(self) -> bool:
        return self.skipped is None and bool(self.checks) and all(c.passed for c in self.checks)

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(passed), detail))
        return bool(passed)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "skipped": self.skipped,
            "checks": [c.to_json() for c in self.checks],
            "observed": self.observed,
        }


class _CameraFault:
    """Stands in for one camera: frozen (the last frame keeps its capture time) or unplugged
    (no frame), while the real camera keeps running behind it."""

    def __init__(self, camera: Any, mode: str) -> None:
        self.camera = camera
        self.mode = mode
        self.frame_lock = threading.Lock()
        with camera.frame_lock:
            self.latest_frame = None if mode == "unplugged" else camera.latest_frame
            self.latest_timestamp = None if mode == "unplugged" else camera.latest_timestamp

    def disconnect(self) -> None:
        self.camera.disconnect()


class LocalSite:
    """The site's gateway process, with its real policy workers, behind the fault proxy, on
    this machine: what the robot connects to during qualification."""

    def __init__(
        self,
        artifact: Path,
        robot_profile: Path,
        execution_profile: Path,
        checkpoint: Path | None,
        device: str,
        workdir: Path,
        start_timeout_s: float = 180.0,
    ) -> None:
        self.workdir = workdir
        gateway_port, status_port, proxy_port = free_port(), free_port(), free_port()
        deployment: dict[str, Any] = {
            "revision_id": REVISION,
            "artifact": str(artifact),
            "robot_profile": str(robot_profile),
            "execution_profile": str(execution_profile),
            "robots": [ROBOT_ID],
            "device": device,
        }
        if checkpoint is not None:
            deployment["checkpoint"] = str(checkpoint)
        else:
            deployment["adapter"] = "mock"
        config = {
            "listen": f"127.0.0.1:{gateway_port}",
            "status_listen": f"127.0.0.1:{status_port}",
            "lease_store": str(workdir / "leases.sqlite"),
            "lease_ttl_ms": 2000,
            "devices": [{"robot_id": ROBOT_ID, "token_sha256": token_digest(TOKEN)}],
            "deployments": [deployment],
            "serving": {
                "backend": "workers",
                "worker_timeout_ms": 2000,
                "worker_start_timeout_s": int(start_timeout_s),
                "worker_dir": str(workdir / "w"),
            },
        }
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / "site.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        self.gateway = GatewayProcess(path, workdir / "gateway.log")
        self.proxy = FaultProxy(proxy_port, gateway_port)
        self.target = f"127.0.0.1:{proxy_port}"
        self.status_url = f"http://127.0.0.1:{status_port}/v1/local/dataplane"

    def status(self) -> dict[str, Any]:
        """The gateway's local status API (empty while it starts)."""
        try:
            with urllib.request.urlopen(self.status_url, timeout=2) as response:  # noqa: S310 - loopback
                status: dict[str, Any] = json.load(response)
        except (OSError, ValueError):
            return {}
        return status

    def workers(self) -> list[dict[str, Any]]:
        groups = self.status().get("groups", {}).values()
        return [g["worker"] for g in groups if "worker" in g]

    async def workers_ready(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            workers = await asyncio.to_thread(self.workers)
            if workers and all(w.get("state") == "ready" for w in workers):
                return True
            await asyncio.sleep(0.25)
        return False

    async def __aenter__(self) -> LocalSite:
        await self.gateway.start()
        await self.proxy.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.proxy.stop()
        await self.gateway.stop()


Scenario = Callable[["Qualification", ScenarioResult], Awaitable[None]]


class Qualification:
    def __init__(self, config: QualifyConfig) -> None:
        self.config = config
        self.binding: ExecutionBinding = load_binding(
            config.artifact, config.robot_profile, config.execution_profile
        )
        self.monitor = InvariantMonitor(clock=monotonic_us, gateway_observed=False)
        self.results: list[ScenarioResult] = []
        self.transitions: list[tuple[int, RuntimeState]] = []
        self.command_times: list[int] = []
        self.fail_commands = False
        self.recording = False
        self.tamper: Callable[[dataplane_pb2.ActionChunk], None] | None = None
        self.last_chunk: dataplane_pb2.GatewayMessage | None = None
        self.pairs: list[tuple[SensorSnapshot, str, NDArray[np.float64]]] = []
        self._observations: dict[tuple[int, int], tuple[SensorSnapshot, str]] = {}
        self._snapshot: SensorSnapshot | None = None
        self.arm: So101Arm | None = None
        self.process: ArmProcess | None = None
        self.said: list[str] = []
        self.parity: dict[str, Any] = {"measured": False}

    # --- the site and the robot -------------------------------------------------------------------

    def _say(self, line: str) -> None:
        self.said.append(line)
        self.config.progress(line)

    def _connect_robot(self, target: str) -> None:
        arm = So101Arm(self.config.arm, self.binding.robot)
        arm.connect()
        self.arm = arm
        command, capture = arm.command, arm.capture

        def command_hook(action: NDArray[np.float64]) -> CommandResult:
            if self.fail_commands:
                return CommandResult(False, "injected controller failure (qualification)")
            result = command(action)
            if result.ok:
                self.command_times.append(monotonic_us())
            return result

        def capture_hook(now_us: int) -> SensorSnapshot | None:
            snapshot = capture(now_us)
            self._snapshot = snapshot
            return snapshot

        arm.command = command_hook  # type: ignore[method-assign]
        arm.capture = capture_hook  # type: ignore[method-assign]
        process = ArmProcess(
            arm,
            self.binding,
            ROBOT_ID,
            REVISION,
            target,
            TOKEN,
            instruction=self.config.instruction,
            say=self._say,
        )
        process.watch(self.monitor)
        process.tick_hooks.append(self._track)
        client = process.client
        drain, on_message = client.drain, client.on_gateway_message

        def drain_hook() -> list[Any]:
            items = drain()
            for item in items:
                message = item.message
                if (
                    self.recording
                    and self._snapshot is not None
                    and isinstance(message, dataplane_pb2.RobotMessage)
                    and message.WhichOneof("body") == "observation"
                ):
                    env = message.observation.envelope
                    self._observations[(env.task_epoch, env.sequence)] = (
                        self._snapshot,
                        message.observation.instruction,
                    )
            return items

        def message_hook(message: dataplane_pb2.GatewayMessage, now_us: int) -> None:
            if message.WhichOneof("body") == "action_chunk":
                chunk = message.action_chunk
                if self.tamper is not None:
                    tamper, self.tamper = self.tamper, None
                    tamper(chunk)
                else:
                    self.last_chunk = _copy(message)
                key = (chunk.envelope.task_epoch, chunk.envelope.sequence)
                seen = self._observations.pop(key, None)
                if seen is not None and len(self.pairs) < self.config.parity_samples:
                    with contextlib.suppress(tensors.TensorError):
                        actions = tensors.decode(
                            chunk.actions,
                            shape=(self.binding.chunk_length, self.binding.action_dim),
                        ).astype(np.float64)
                        self.pairs.append((seen[0], seen[1], actions))
            on_message(message, now_us)

        client.drain = drain_hook  # type: ignore[method-assign]
        client.on_gateway_message = message_hook  # type: ignore[method-assign]
        self.process = process

    def _track(self, now_us: int) -> None:
        assert self.process is not None
        state = self.process.runtime.state
        if not self.transitions or self.transitions[-1][1] is not state:
            self.transitions.append((now_us, state))

    # --- helpers for scenarios --------------------------------------------------------------------

    @property
    def runtime(self) -> Any:
        assert self.process is not None
        return self.process.runtime

    def execute(self, line: str) -> str:
        """An operator command, on the control loop's thread (this event loop)."""
        assert self.process is not None
        reply = self.process.execute(line, monotonic_us())
        self._say(reply)
        return reply

    async def until(self, condition: Callable[[], bool], timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while not condition():
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(0.02)
        return True

    async def running(self, timeout_s: float = 60.0, actions: int = 15) -> bool:
        """Armed and executing: reset a latched fault, arm when Ready, wait for actions."""
        deadline = time.monotonic() + timeout_s
        start = self.runtime.dispatched
        while time.monotonic() < deadline:
            state = self.runtime.state
            if state is RuntimeState.FAULTED:
                self.execute("reset")
            elif state is RuntimeState.READY:
                self.execute("arm")
            elif state is RuntimeState.RUNNING and self.runtime.dispatched - start >= actions:
                return True
            await asyncio.sleep(0.1)
        return False

    async def ready(self, timeout_s: float = 60.0) -> bool:
        """Back at a task boundary with a session: after a fault, an explicit reset."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            state = self.runtime.state
            if state is RuntimeState.READY:
                return True
            if state is RuntimeState.FAULTED:
                self.execute("reset")
            elif state in (RuntimeState.ARMED, RuntimeState.RUNNING, RuntimeState.WAITING):
                with contextlib.suppress(TransitionError):
                    self.runtime.complete_task(monotonic_us())
            await asyncio.sleep(0.1)
        return False

    def states_between(self, start_us: int, end_us: int) -> list[tuple[int, RuntimeState]]:
        before = [t for t in self.transitions if t[0] <= start_us][-1:]
        return before + [t for t in self.transitions if start_us < t[0] <= end_us]

    def first(self, state: RuntimeState, start_us: int, end_us: int) -> int | None:
        return next(
            (t for t, s in self.transitions if s is state and start_us <= t <= end_us), None
        )

    def commands_between(self, start_us: int, end_us: int) -> int:
        return sum(1 for t in self.command_times if start_us < t <= end_us)

    def drift(self, start_us: int, end_us: int, exclude: tuple[str, ...] = ()) -> tuple[float, int]:
        """Largest movement of any joint between ``start_us + settle`` and ``end_us``, and how
        many readings that is based on."""
        assert self.arm is not None
        begin = start_us + int(self.config.settle_s * 1e6)
        samples = [s for s in self.arm.telemetry if begin <= s.at_us <= end_us]
        if len(samples) < 2:
            return 0.0, len(samples)
        first = samples[0].measured
        worst = 0.0
        for sample in samples[1:]:
            for joint, value in sample.measured.items():
                if joint.removesuffix(".pos") not in exclude:
                    worst = max(worst, abs(value - first[joint]))
        return worst, len(samples)

    def judge_window(
        self,
        result: ScenarioResult,
        start_us: int,
        end_us: int,
        *,
        must_fault: set[ReasonCode] | None = None,
        must_confirm: bool = True,
        exclude: tuple[str, ...] = (),
    ) -> None:
        """What a fault must leave behind: a latched fault with a confirmed response when one
        is required, every starved or faulted interval without commands and with the arm still,
        and no invariant violation."""
        states = self.states_between(start_us, end_us)
        result.observed["states"] = [s.value for _, s in states]
        fault_at = self.first(RuntimeState.FAULTED, start_us, end_us)
        fault = self.runtime.fault
        if must_fault is not None:
            reasons = sorted(r.value for r in must_fault)
            if not result.check(
                "faulted",
                fault_at is not None and fault is not None and fault.reason in must_fault,
                f"expected {reasons}, got {fault.reason.value if fault else 'no fault'}",
            ):
                return
        if fault_at is not None and fault is not None:
            result.observed["fault"] = {
                "reason": fault.reason.value,
                "response": fault.response.value if fault.response else None,
                "confirmed": fault.response_confirmed,
                "detail": fault.detail,
            }
            if must_confirm and fault.reason is not ReasonCode.ACTUATOR_LOCK_LOST:
                result.check(
                    "response confirmed",
                    fault.response_confirmed,
                    f"{fault.response.value if fault.response else 'none'} on the arm",
                )
            elif not must_confirm:
                result.check(
                    "no stop claimed without confirmation",
                    not fault.response_confirmed,
                    "the runtime must not report a stop the arm could not confirm",
                )
            result.check(
                "latched",
                self.runtime.state is RuntimeState.FAULTED,
                "only an explicit local reset clears a fault",
            )
            sent = self.commands_between(fault_at, end_us)
            result.check("no command after the fault", sent == 0, f"{sent} commands")
            if must_confirm:
                moved, readings = self.drift(fault_at, end_us, exclude)
                result.check(
                    "held still",
                    moved <= self.config.hold_tolerance and readings >= 5,
                    f"moved {moved:.2f} (tolerance {self.config.hold_tolerance}) over "
                    f"{readings} readings",
                )
        for begin, finish in self._intervals(RuntimeState.WAITING, start_us, end_us):
            sent = self.commands_between(begin, finish)
            result.check("no command while starved", sent == 0, f"{sent} commands")
            if finish - begin > int(self.config.settle_s * 1e6) + 200_000:
                moved, readings = self.drift(begin, finish, exclude)
                result.check(
                    "held still while starved",
                    moved <= self.config.hold_tolerance,
                    f"moved {moved:.2f} over {readings} readings",
                )

    def _intervals(self, state: RuntimeState, start_us: int, end_us: int) -> list[tuple[int, int]]:
        out = []
        states = self.states_between(start_us, end_us)
        for i, (at, s) in enumerate(states):
            if s is state:
                finish = states[i + 1][0] if i + 1 < len(states) else end_us
                out.append((max(at, start_us), finish))
        return out

    async def disturb(
        self,
        result: ScenarioResult,
        inject: Callable[[], Awaitable[Any]],
        *,
        observe_s: float = 3.0,
        must_fault: set[ReasonCode] | None = None,
        must_confirm: bool = True,
        exclude: tuple[str, ...] = (),
        must_not_fault: bool = False,
        recover: bool = True,
    ) -> None:
        if not result.check("running before the fault", await self.running()):
            return
        violations = self.monitor.violations
        start = monotonic_us()
        await inject()
        await asyncio.sleep(observe_s)
        end = monotonic_us()
        self.judge_window(
            result, start, end, must_fault=must_fault, must_confirm=must_confirm, exclude=exclude
        )
        result.check(
            "no invariant violation",
            self.monitor.violations == violations,
            f"{self.monitor.violations - violations} violations",
        )
        if must_not_fault:
            result.check(
                "kept running",
                self.first(RuntimeState.FAULTED, start, end) is None
                and self.runtime.state is RuntimeState.RUNNING,
                f"ended {self.runtime.state.value}",
            )
        if recover:
            result.check("recovers after an operator reset", await self.ready(timeout_s=120))
        else:
            reply = self.execute("reset")
            result.check("reset is refused", reply.startswith("reset refused"), reply)

    # --- the run ----------------------------------------------------------------------------------

    def applicable(self) -> list[tuple[str, Scenario]]:
        emulated = self.config.emulator is not None
        chosen = []
        for name, (scenario, needs) in SCENARIOS.items():
            if self.config.scenarios and name not in self.config.scenarios:
                continue
            if needs == "emulator" and not emulated:
                self.results.append(
                    ScenarioResult(name, skipped="needs the emulator: it cannot be induced safely")
                )
                continue
            if needs == "manual" and not (emulated or self.config.manual):
                self.results.append(ScenarioResult(name, skipped="manual: run with --manual"))
                continue
            chosen.append((name, scenario))
        return chosen

    async def run(self) -> dict[str, Any]:
        config = self.config
        started = time.time()
        with contextlib.ExitStack() as stack:
            if config.workdir is None:
                # Short on purpose: worker Unix socket paths have a length limit.
                workdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="nq-")))
            else:
                workdir = config.workdir
            site = LocalSite(
                config.artifact,
                config.robot_profile,
                config.execution_profile,
                config.checkpoint,
                config.device,
                workdir,
                config.start_timeout_s,
            )
            self.gateway, self.proxy, self.target = site.gateway, site.proxy, site.target
            async with site:
                await self._serve()
        self.measure_parity()
        return self.report(started, time.time())

    async def _serve(self) -> None:
        config = self.config
        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        try:
            self._connect_robot(self.target)
            assert self.process is not None
            task = asyncio.create_task(self.process.connection.run(stop))
            if not await self.until(
                lambda: self.runtime.state is RuntimeState.READY, config.start_timeout_s
            ):
                raise RuntimeError(f"the robot did not get a session; see {self.gateway.log_path}")
            if not config.confirm(
                "The arm will now move under the policy, and faults will be injected. Is the "
                "operator at the power switch, with the workspace clear?"
            ):
                raise RuntimeError("the operator did not confirm")
            for name, scenario in self.applicable():
                result = ScenarioResult(name)
                self._say(f"--- {name}")
                try:
                    await scenario(self, result)
                except Exception as exc:  # a broken scenario fails, the run goes on
                    result.check("ran", False, f"{type(exc).__name__}: {exc}")
                self.results.append(result)
                self._say(f"{name}: {'passed' if result.passed else 'FAILED'}")
                if name == "actuator_lock_lost":
                    break  # the arm is no longer this process's
                if not await self.ready(timeout_s=120):
                    self._say("the robot did not recover; stopping the run")
                    break
        finally:
            stop.set()
            if task is not None:
                await task
            if self.arm is not None:
                self.arm.close()

    # --- parity -----------------------------------------------------------------------------------

    def measure_parity(self) -> None:
        if self.config.checkpoint is None:
            self.parity = {"measured": False, "reason": "no checkpoint (mock policy)"}
            return
        if not self.pairs:
            self.parity = {"measured": False, "reason": "no observation and chunk recorded"}
            return
        from neura_robots.qualify.reference import LeRobotReference

        reference = LeRobotReference(
            self.binding.artifact,
            self.binding.robot,
            self.config.checkpoint,
            device=self.config.device,
        )
        errors = []
        for snapshot, instruction, served in self.pairs:
            expected = reference.chunk(snapshot, instruction)
            n = min(len(expected), len(served))
            errors.append(float(np.max(np.abs(served[:n] - expected[:n]))))
        worst = max(errors)
        self.parity = {
            "measured": True,
            "samples": len(errors),
            "max_abs_error": worst,
            "mean_max_abs_error": float(np.mean(errors)),
            "atol": self.config.parity_atol,
            "passed": worst <= self.config.parity_atol,
            "reference": "LeRobot alone on the same arm readings (predict_action_chunk)",
        }

    # --- the report -------------------------------------------------------------------------------

    def report(self, started: float, finished: float) -> dict[str, Any]:
        global_checks = self._global_checks()
        scenarios_passed = all(r.passed for r in self.results if r.skipped is None)
        complete = {r.name for r in self.results if r.skipped is None} == set(SCENARIOS)
        parity_passed = bool(self.parity.get("passed"))
        emulated = self.config.emulator is not None
        return {
            "kind": "neura-hardware-qualification",
            "version": 1,
            "started_at": _iso(started),
            "duration_s": round(finished - started, 1),
            "emulated": emulated,
            "passed": scenarios_passed
            and parity_passed
            and all(c.passed for c in global_checks)
            and self.monitor.violations == 0,
            "complete": complete,
            "hardware": self._hardware(),
            "software": _software(),
            "contracts": {
                "model": self.binding.artifact.metadata.name,
                "artifact": self.binding.artifact_digest,
                "robot_profile": self.binding.robot.digest(),
                "robot": self.binding.robot.metadata.name,
                "execution_profile": self.binding.execution_profile_digest,
                "execution_profile_name": self.binding.profile.metadata.name,
                "device": self.config.device,
            },
            "scenarios": [r.to_json() for r in self.results],
            "global_checks": [c.to_json() for c in global_checks],
            "parity": self.parity,
            "invariants": self.monitor.summary(),
            "arm_counters": dict(self.arm.counters) if self.arm else {},
        }

    def _global_checks(self) -> list[Check]:
        checks = []
        if self.arm is not None:
            low = dict(zip(self.arm.commands, self.binding.action_transform.low, strict=True))
            high = dict(zip(self.arm.commands, self.binding.action_transform.high, strict=True))
            outside = [
                (k, v)
                for sample in self.arm.telemetry
                for k, v in (sample.commanded or {}).items()
                if k in low and not low[k] - 1e-6 <= v <= high[k] + 1e-6
            ]
            checks.append(
                Check(
                    "every command within the profile's ranges",
                    not outside,
                    f"{len(outside)} outside" + (f", first {outside[0]}" if outside else ""),
                )
            )
        return checks

    def _hardware(self) -> dict[str, Any]:
        arm = self.arm
        out: dict[str, Any] = {"arm": "so101", "port": self.config.arm.port}
        out["calibration_id"] = self.config.arm.calibration_id
        out["cameras"] = {
            role: {"kind": c.kind, "fps": c.fps} for role, c in self.config.arm.cameras.items()
        }
        if arm is not None:
            out["servo_firmware"] = arm.servo_firmware
        return out


def _copy(message: dataplane_pb2.GatewayMessage) -> dataplane_pb2.GatewayMessage:
    copy = dataplane_pb2.GatewayMessage()
    copy.CopyFrom(message)
    return copy


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def _software() -> dict[str, str]:
    from importlib import metadata

    out = {"python": platform.python_version(), "platform": platform.platform()}
    for package in ("neura-robots", "neura-runtime", "neura-server", "lerobot", "torch"):
        with contextlib.suppress(metadata.PackageNotFoundError):
            out[package] = metadata.version(package)
    return out


# --- scenarios ------------------------------------------------------------------------------------


async def nominal(q: Qualification, result: ScenarioResult) -> None:
    if not result.check("running", await q.running(timeout_s=q.config.start_timeout_s)):
        return
    runtime = q.runtime
    start, dispatched = monotonic_us(), runtime.dispatched
    ticks, late = q.process.connection.ticks, q.process.connection.late_ticks  # type: ignore[union-attr]
    violations = q.monitor.violations
    q.recording = True
    await asyncio.sleep(q.config.nominal_s)
    q.recording = False
    end = monotonic_us()
    period = q.binding.control_period_us
    expected = (end - start) / period
    actions = runtime.dispatched - dispatched
    connection = q.process.connection  # type: ignore[union-attr]
    late_share = (connection.late_ticks - late) / max(1, connection.ticks - ticks)
    result.observed.update(
        {
            "seconds": round((end - start) / 1e6, 1),
            "actions": actions,
            "expected_actions": round(expected),
            "late_tick_share": round(late_share, 4),
            "rtt_p99_us": q.process.client.rtt.p99_us(),  # type: ignore[union-attr]
            "parity_samples": len(q.pairs),
        }
    )
    result.check("no fault", q.first(RuntimeState.FAULTED, start, end) is None)
    result.check(
        "executes at the control rate",
        actions >= 0.8 * expected,
        f"{actions} actions of {expected:.0f} control periods",
    )
    result.check("control loop on time", late_share <= 0.02, f"{late_share:.2%} late ticks")
    result.check("no invariant violation", q.monitor.violations == violations)


async def network_jitter(q: Qualification, result: ScenarioResult) -> None:
    """Latency within the budget: the robot must keep running."""

    async def inject() -> None:
        q.proxy.delay_s = 0.02
        await asyncio.sleep(4.0)
        q.proxy.delay_s = 0.0

    await q.disturb(result, inject, observe_s=1.0, must_not_fault=True)


async def network_delay(q: Qualification, result: ScenarioResult) -> None:
    """Latency above the budget: replies arrive too old to execute."""

    async def inject() -> None:
        q.proxy.delay_s = 0.4
        await asyncio.sleep(4.0)
        q.proxy.delay_s = 0.0

    await q.disturb(result, inject)


async def partition(q: Qualification, result: ScenarioResult) -> None:
    async def inject() -> None:
        q.proxy.partition(5.0)
        await asyncio.sleep(5.0)

    await q.disturb(result, inject, must_fault={ReasonCode.STARVATION_GRACE_EXHAUSTED})


async def reset_connections(q: Qualification, result: ScenarioResult) -> None:
    async def inject() -> None:
        q.proxy.reset()

    await q.disturb(result, inject)


async def gateway_kill(q: Qualification, result: ScenarioResult) -> None:
    async def inject() -> None:
        q.gateway.signal(signal.SIGKILL)

    await q.disturb(result, inject, observe_s=5.0)


async def gateway_freeze(q: Qualification, result: ScenarioResult) -> None:
    async def inject() -> None:
        pid = q.gateway.pid
        if pid is None:
            return
        os.killpg(pid, signal.SIGSTOP)
        try:
            await asyncio.sleep(2.5)
        finally:
            os.killpg(pid, signal.SIGCONT)

    await q.disturb(result, inject)


async def worker_kill(q: Qualification, result: ScenarioResult) -> None:
    async def inject() -> None:
        workers = q.gateway.workers()
        result.observed["workers"] = len(workers)
        for pid in workers:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    await q.disturb(result, inject, observe_s=5.0)


async def operator_takeover(q: Qualification, result: ScenarioResult) -> None:
    if not result.check("running before the takeover", await q.running()):
        return
    reply = q.execute("takeover")
    taken = monotonic_us()
    result.check("ready at once", q.runtime.state is RuntimeState.READY, q.runtime.state.value)
    await asyncio.sleep(2.0)
    end = monotonic_us()
    result.observed["reply"] = reply
    result.check("hold confirmed", reply == "taken over; holding")
    sent = q.commands_between(taken, end)
    result.check("no command after the takeover", sent == 0, f"{sent} commands")
    moved, readings = q.drift(taken, end)
    result.check(
        "held still",
        moved <= q.config.hold_tolerance and readings >= 5,
        f"moved {moved:.2f} over {readings} readings",
    )


async def second_client(q: Qualification, result: ScenarioResult) -> None:
    other = SimulatedRobotProcess(q.binding, ROBOT_ID, REVISION, q.target, TOKEN)
    other.watch(q.monitor)
    stop = asyncio.Event()

    async def inject() -> None:
        task = asyncio.create_task(other.connection.run(stop))
        await asyncio.sleep(6.0)
        stop.set()
        await task
        result.observed["second_client_dispatched"] = other.runtime.dispatched

    await q.disturb(result, inject)


async def camera_frozen(q: Qualification, result: ScenarioResult) -> None:
    await _camera(q, result, "frozen")


async def camera_unplugged(q: Qualification, result: ScenarioResult) -> None:
    await _camera(q, result, "unplugged")


async def _camera(q: Qualification, result: ScenarioResult, mode: str) -> None:
    assert q.arm is not None
    if not q.arm.cameras:
        result.skipped = "the robot profile has no camera"
        return
    role = sorted(q.arm.cameras)[-1]
    result.observed["camera"] = role
    incomplete = q.runtime.counters[ReasonCode.OBSERVATION_INCOMPLETE.value]

    async def inject() -> None:
        assert q.arm is not None
        real = q.arm.cameras[role]
        q.arm.cameras[role] = _CameraFault(real, mode)
        try:
            await asyncio.sleep(3.0)
        finally:
            q.arm.cameras[role] = real

    await q.disturb(result, inject, observe_s=1.0)
    refused = q.runtime.counters[ReasonCode.OBSERVATION_INCOMPLETE.value] - incomplete
    result.check("observations refused", refused > 0, f"{refused} incomplete observations")


async def invalid_action(q: Qualification, result: ScenarioResult) -> None:
    transform = q.binding.action_transform

    def tamper(chunk: dataplane_pb2.ActionChunk) -> None:
        model = tensors.decode(
            chunk.actions, shape=(q.binding.chunk_length, q.binding.action_dim)
        ).astype(np.float64)
        robot = transform.to_robot(model)
        robot[0, 0] = transform.high[0] + 30.0  # well outside the first joint's range
        actions = tensors.encode(transform.to_model(robot).astype(np.float32))
        chunk.actions.CopyFrom(actions)
        chunk.actions_digest.sha256 = tensors.payload_digest(actions.data)  # a valid digest

    async def inject() -> None:
        q.tamper = tamper
        await q.until(lambda: q.tamper is None, 5.0)

    await q.disturb(result, inject, must_fault={ReasonCode.ACTION_OUT_OF_RANGE})


async def nan_action(q: Qualification, result: ScenarioResult) -> None:
    rejected = q.runtime.counters[ReasonCode.CHUNK_INTEGRITY_FAILED.value]

    def tamper(chunk: dataplane_pb2.ActionChunk) -> None:
        model = tensors.decode(
            chunk.actions, shape=(q.binding.chunk_length, q.binding.action_dim)
        ).astype(np.float32)
        model[0, 0] = np.nan
        actions = tensors.encode(model)
        chunk.actions.CopyFrom(actions)
        chunk.actions_digest.sha256 = tensors.payload_digest(actions.data)

    async def inject() -> None:
        q.tamper = tamper
        await q.until(lambda: q.tamper is None, 5.0)

    await q.disturb(result, inject)
    refused = q.runtime.counters[ReasonCode.CHUNK_INTEGRITY_FAILED.value] - rejected
    result.check("the chunk was refused", refused >= 1, f"{refused} refused")


async def duplicate_chunk(q: Qualification, result: ScenarioResult) -> None:
    duplicates = q.runtime.counters[ReasonCode.CHUNK_DUPLICATE.value]

    async def inject() -> None:
        assert q.process is not None
        await q.until(lambda: q.last_chunk is not None, 5.0)
        if q.last_chunk is not None:
            q.process.client.on_gateway_message(_copy(q.last_chunk), monotonic_us())

    await q.disturb(result, inject, observe_s=1.0)
    seen = q.runtime.counters[ReasonCode.CHUNK_DUPLICATE.value] - duplicates
    result.check("the duplicate was dropped", seen >= 1, f"{seen} duplicates dropped")
    result.check("no fault", q.runtime.fault is None)


async def old_epoch_chunk(q: Qualification, result: ScenarioResult) -> None:
    mismatched = q.runtime.counters[ReasonCode.CHUNK_EPOCH_MISMATCH.value]
    if not result.check("running", await q.running()):
        return
    await q.until(lambda: q.last_chunk is not None, 5.0)
    saved = q.last_chunk
    q.execute("takeover")
    if not result.check("running again in a new epoch", await q.running()):
        return
    assert q.process is not None
    assert saved is not None
    start = monotonic_us()
    q.process.client.on_gateway_message(_copy(saved), monotonic_us())
    await asyncio.sleep(1.0)
    seen = q.runtime.counters[ReasonCode.CHUNK_EPOCH_MISMATCH.value] - mismatched
    result.check("the old epoch's chunk was dropped", seen >= 1, f"{seen} dropped")
    result.check("no fault", q.first(RuntimeState.FAULTED, start, monotonic_us()) is None)


async def controller_failure(q: Qualification, result: ScenarioResult) -> None:
    async def inject() -> None:
        q.fail_commands = True
        await q.until(lambda: q.runtime.state is RuntimeState.FAULTED, 3.0)
        q.fail_commands = False

    await q.disturb(result, inject, must_fault={ReasonCode.CONTROLLER_COMMAND_FAILED})


async def torque_loss(q: Qualification, result: ScenarioResult) -> None:
    emulator = q.config.emulator
    assert emulator is not None
    joint = "elbow_flex"

    async def inject() -> None:
        emulator.set_fault(joint, overload=True)

    # The tripped joint sags (gravity): the hold cannot be confirmed, and must not be claimed.
    await q.disturb(
        result,
        inject,
        must_fault={ReasonCode.CONTROLLER_COMMAND_FAILED},
        must_confirm=False,
        exclude=(joint,),
    )


async def bus_unplugged(q: Qualification, result: ScenarioResult) -> None:
    emulator = q.config.emulator

    async def inject() -> None:
        if emulator is not None:
            emulator.set_fault(silent=True)
        elif not q.config.confirm("Unplug the servo bus USB cable now, then confirm."):
            raise RuntimeError("the operator did not unplug the bus")
        await q.until(lambda: q.runtime.state is RuntimeState.FAULTED, 5.0)
        if emulator is not None:
            await asyncio.sleep(1.0)
            emulator.set_fault(silent=False)
        elif not q.config.confirm("Plug the servo bus back in, then confirm."):
            raise RuntimeError("the operator did not reconnect the bus")

    await q.disturb(
        result,
        inject,
        must_fault={ReasonCode.CONTROLLER_COMMAND_FAILED, ReasonCode.STARVATION_GRACE_EXHAUSTED},
        must_confirm=False,
    )


async def second_runtime_blocked(q: Qualification, result: ScenarioResult) -> None:
    if not result.check("running", await q.running()):
        return
    other = So101Arm(q.config.arm, q.binding.robot)
    try:
        other.connect()
        blocked, detail = False, "a second runtime connected to the arm"
        other.close(hold=True)
    except ActuatorBusyError as exc:
        blocked, detail = True, str(exc)
    result.check("a second runtime is refused", blocked, detail)
    try:
        fd = os.open(q.config.arm.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        result.observed["direct_open"] = f"refused: {exc.strerror}"
    else:
        os.close(fd)
        # TIOCEXCL does not bind a privileged process; the lock file still does.
        result.observed["direct_open"] = (
            "allowed (privileged process)" if os.geteuid() == 0 else ("allowed")
        )
        if os.geteuid() != 0:
            result.check("the serial port is exclusive", False, "another process opened it")
    await asyncio.sleep(0.5)
    result.check("still running", q.runtime.state is RuntimeState.RUNNING)
    assert q.arm is not None
    result.check("still holds the actuator", q.arm.has_authority())


async def actuator_lock_lost(q: Qualification, result: ScenarioResult) -> None:
    assert q.arm is not None
    lock = q.arm.lock

    async def inject() -> None:
        lock.path.unlink()
        lock.path.write_text("another process\n")

    # The arm may be someone else's now: no local response is applied, and no reset can
    # bring it back (only a restart that takes the lock again).
    await q.disturb(
        result, inject, must_fault={ReasonCode.ACTUATOR_LOCK_LOST}, observe_s=1.5, recover=False
    )


SCENARIOS: dict[str, tuple[Scenario, str]] = {
    "nominal": (nominal, "any"),
    "network_jitter": (network_jitter, "any"),
    "network_delay": (network_delay, "any"),
    "partition": (partition, "any"),
    "reset_connections": (reset_connections, "any"),
    "gateway_kill": (gateway_kill, "any"),
    "gateway_freeze": (gateway_freeze, "any"),
    "worker_kill": (worker_kill, "any"),
    "operator_takeover": (operator_takeover, "any"),
    "second_client": (second_client, "any"),
    "camera_frozen": (camera_frozen, "any"),
    "camera_unplugged": (camera_unplugged, "any"),
    "invalid_action": (invalid_action, "any"),
    "nan_action": (nan_action, "any"),
    "duplicate_chunk": (duplicate_chunk, "any"),
    "old_epoch_chunk": (old_epoch_chunk, "any"),
    "controller_failure": (controller_failure, "any"),
    "torque_loss": (torque_loss, "emulator"),
    "bus_unplugged": (bus_unplugged, "manual"),
    "second_runtime_blocked": (second_runtime_blocked, "any"),
    "actuator_lock_lost": (actuator_lock_lost, "any"),  # last: the arm is no longer ours
}


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
