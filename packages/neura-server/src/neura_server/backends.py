"""What runs a batch group's forward passes, as seen from the gateway's dispatcher.

`WorkerBackend` supervises one policy worker process (`neura_server.worker`) per batch group and
talks to it over a Unix socket in a directory made for that run alone (design §9.1, §12), once the
socket has said it serves the artifact asked for. It restarts a worker that exits or hangs, and
never delivers a result from a worker incarnation other than the one the batch was sent to: a
batch in flight when its worker dies fails with WORKER_CRASHED, one whose worker hangs fails with
WORKER_TIMED_OUT, and while no worker is ready batches fail with WORKER_UNAVAILABLE. Robots get
these as typed rejections and keep their own safety behavior.

`InProcessBackend` runs a policy in a thread of the gateway process: the mock policy for
development stacks and tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import grpc
import numpy as np
from numpy.typing import NDArray

from neura.v1 import worker_pb2, worker_pb2_grpc
from neura_adapters import PolicyBackend
from neura_contracts import ReasonCode, load_document
from neura_runtime.policy import ModelInputs, PolicyError
from neura_server.sandbox import Invocation, Launcher, ProcessLauncher, private_directory
from neura_server.supervision import WORKER_STOP_TIMEOUT_S, RestartBackoff
from neura_server.worker import MAX_MESSAGE_BYTES, encode_inputs, outputs_from

log = logging.getLogger("neura.gateway.workers")
UNIX_PATH_MAX = 107
SOCKET = "sock"  # in its run's own directory: "<group>-<incarnation>/sock", as short as before


class Backend(Protocol):
    async def start(self) -> None: ...

    async def infer(
        self, batch_id: int, batch: Sequence[ModelInputs], *, timeout_s: float
    ) -> list[NDArray[np.float32]]:
        """Raises PolicyError (with the reason robots receive) when the batch fails."""
        ...

    async def stop(self) -> None: ...

    def status(self) -> dict[str, Any]: ...


class InProcessBackend:
    def __init__(self, policy: PolicyBackend) -> None:
        self._policy = policy

    async def start(self) -> None:
        return None

    async def infer(
        self, batch_id: int, batch: Sequence[ModelInputs], *, timeout_s: float
    ) -> list[NDArray[np.float32]]:
        del batch_id, timeout_s
        try:
            return await asyncio.to_thread(self._policy.infer, batch)
        except PolicyError:
            raise
        except MemoryError as exc:
            raise PolicyError(str(exc), out_of_memory=True) from exc
        except Exception as exc:
            log.exception("policy backend failed")
            raise PolicyError(f"{type(exc).__name__}: {exc}") from exc

    async def stop(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {"backend": "simulated", "state": "ready"}


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    """Everything a worker process needs to load its model."""

    artifact: Path
    robot_profile: Path
    execution_profile: Path
    device: str
    checkpoint: Path | None = None
    adapter: str | None = None
    warmup_batches: tuple[int, ...] = (1,)

    def invocation(self, directory: Path) -> Invocation:
        """``neura-worker serve`` for this spec, listening on a socket in ``directory``, the run's
        own (the only one it may write)."""
        flags = ["--device", self.device, "--warmup", ",".join(str(b) for b in self.warmup_batches)]
        if self.adapter is not None:
            flags += ["--adapter", self.adapter]
        inputs = [
            ("--artifact", self.artifact),
            ("--robot-profile", self.robot_profile),
            ("--execution-profile", self.execution_profile),
        ]
        if self.checkpoint is not None:
            inputs.append(("--checkpoint", self.checkpoint))
        return Invocation("serve", self.device, flags, inputs, directory, [("--socket", SOCKET)])


class WorkerBackend:
    def __init__(
        self,
        name: str,
        spec: WorkerSpec,
        *,
        socket_dir: Path,
        start_timeout_s: float,
        command: Sequence[str] | None = None,
        launcher: Launcher | None = None,
    ) -> None:
        if (command is None) == (launcher is None):
            raise ValueError("give a worker command or a launcher")
        self.name = name
        self.spec = spec
        self.launcher: Launcher = launcher or ProcessLauncher(command or [])
        self._socket_dir = socket_dir
        self._start_timeout_s = start_timeout_s
        if len(str(self._run_dir(999_999) / SOCKET)) > UNIX_PATH_MAX:
            raise ValueError(f"worker socket directory {socket_dir} is too long for Unix sockets")
        self.state = "stopped"  # starting | ready | restarting | failed | stopped
        self.incarnation = 0
        self.restarts = 0
        self.last_error: dict[str, str] | None = None
        self.description: dict[str, Any] | None = None
        self.peak_memory_mib = 0
        self._process: asyncio.subprocess.Process | None = None
        self._channel: grpc.aio.Channel | None = None
        self._stub: worker_pb2_grpc.PolicyWorkerStub | None = None
        self._supervisor: asyncio.Task[None] | None = None
        # Container removals under way: shielded from the supervisor's cancellation, so stop()
        # waits for them itself.
        self._removals: set[asyncio.Future[None]] = set()
        self._stopping = False
        self.ready = asyncio.Event()

    def _run_dir(self, incarnation: int) -> Path:
        return self._socket_dir / f"{self.name}-{incarnation}"

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        if self._supervisor is None:
            self._stopping = False
            self._supervisor = asyncio.create_task(self._supervise(), name=f"worker-{self.name}")

    async def stop(self) -> None:
        self._stopping = True
        process = self._process
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                process.stdin.close()  # the worker exits when its stdin closes
            if self.state != "ready":
                process.kill()  # loading, or failed to: nothing of its to finish
            try:
                await asyncio.wait_for(process.wait(), timeout=WORKER_STOP_TIMEOUT_S)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        if self._removals:
            await asyncio.gather(*self._removals, return_exceptions=True)
        await self._disconnect()
        self.state = "stopped"

    def _stop_requested(self) -> bool:
        return self._stopping

    async def _supervise(self) -> None:
        backoff = RestartBackoff(first_s=0.5, max_s=30.0)
        while not self._stop_requested():
            self.incarnation += 1
            directory = private_directory(self._run_dir(self.incarnation))
            self.state = "starting" if self.restarts == 0 else "restarting"
            backoff.started()
            run = directory.name
            try:
                process = self._process = await asyncio.create_subprocess_exec(
                    *self.launcher.command(run, self.spec.invocation(directory)),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                )
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            log.info("worker %s starting (pid %s) on %s", self.name, process.pid, self.spec.device)
            drain: asyncio.Task[None] | None = None
            try:
                announced = await self._await_announcement(process)
                if "ready" in announced and (problem := await self._connect(directory)):
                    process.kill()
                    announced = {
                        "error": {"code": ReasonCode.WORKER_LOAD_FAILED.value, "message": problem}
                    }
                if "ready" in announced:
                    self.description = announced["ready"]
                    self.last_error = None
                    self.state = "ready"
                    self.ready.set()
                    log.info("worker %s ready: %s", self.name, announced["ready"])
                else:
                    self.last_error = announced["error"]
                    self.state = "failed"
                    log.error("worker %s could not start: %s", self.name, self.last_error)
                drain = asyncio.create_task(self._drain(process))
                code = await process.wait()
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            finally:
                self.ready.clear()
                self.incarnation += 1  # results from the old incarnation are never delivered
                await self._disconnect()
                if drain is not None:
                    drain.cancel()
                # A killed launcher process (a hung worker) can leave its container running; the
                # run's directory goes once the container has.
                removal = asyncio.ensure_future(self._remove(run, directory))
                self._removals.add(removal)
                removal.add_done_callback(self._removals.discard)
                await asyncio.shield(removal)
            if self._stop_requested():  # stop() may have run while we waited
                return
            self.restarts += 1
            if self.state == "ready":
                self.last_error = {
                    "code": ReasonCode.WORKER_CRASHED.value,
                    "message": f"worker exited with {code}",
                }
            if self.state != "failed":
                self.state = "restarting"
            delay = backoff.exited()
            log.warning("worker %s exited with %s; restarting in %.1fs", self.name, code, delay)
            await asyncio.sleep(delay)

    async def _remove(self, run: str, directory: Path) -> None:
        try:
            await self.launcher.remove(run)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    async def _await_announcement(self, process: asyncio.subprocess.Process) -> dict[str, Any]:
        """The worker's ready or error line. Other output before it is logged and skipped."""
        assert process.stdout is not None
        deadline = time.monotonic() + self._start_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise TimeoutError
                line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            except TimeoutError:
                process.kill()
                return {
                    "error": {
                        "code": ReasonCode.WORKER_LOAD_FAILED.value,
                        "message": f"not ready after {self._start_timeout_s:.0f} s",
                    }
                }
            if not line:
                await process.wait()
                return {
                    "error": {
                        "code": ReasonCode.WORKER_LOAD_FAILED.value,
                        "message": f"exited with {process.returncode} before it was ready",
                    }
                }
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            if isinstance(message, dict) and ("ready" in message or "error" in message):
                return message
            log.info("worker %s: %s", self.name, line.decode(errors="replace").rstrip())

    async def _drain(self, process: asyncio.subprocess.Process) -> None:
        """Keep reading stdout so a chatty library can never block the worker on a full pipe."""
        assert process.stdout is not None
        while line := await process.stdout.readline():
            log.info("worker %s: %s", self.name, line.decode(errors="replace").rstrip())

    async def _connect(self, directory: Path) -> str | None:
        """Connect to the run's socket. None once it answers for the artifact this group serves;
        otherwise why it is not used."""
        self._channel = grpc.aio.insecure_channel(
            f"unix://{directory / SOCKET}",
            options=[
                ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
                ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
            ],
        )
        self._stub = worker_pb2_grpc.PolicyWorkerStub(self._channel)
        try:
            described = await self._stub.Describe(worker_pb2.DescribeRequest(), timeout=10)
            expected = load_document(self.spec.artifact.read_text(encoding="utf-8")).digest()
        except grpc.aio.AioRpcError as exc:
            problem = f"announced ready, but its socket does not answer: {exc.code().name}"
        except (OSError, ValueError) as exc:
            problem = f"cannot read the artifact it was asked to serve: {exc}"
        else:
            if described.artifact_digest == expected:
                return None
            problem = f"its socket answers for artifact {described.artifact_digest}, not {expected}"
        await self._disconnect()
        return problem

    async def _disconnect(self) -> None:
        channel, self._channel, self._stub = self._channel, None, None
        if channel is not None:
            await channel.close(grace=None)

    def _kill(self, why: str) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            log.error("worker %s %s; killing pid %s", self.name, why, process.pid)
            process.kill()

    # --- inference ----------------------------------------------------------------------------

    async def infer(
        self, batch_id: int, batch: Sequence[ModelInputs], *, timeout_s: float
    ) -> list[NDArray[np.float32]]:
        stub = self._stub
        if self.state != "ready" or stub is None:
            raise PolicyError(
                f"worker {self.name} is {self.state}", reason=ReasonCode.WORKER_UNAVAILABLE
            )
        incarnation = self.incarnation
        request = worker_pb2.InferRequest(
            batch_id=batch_id, inputs=[encode_inputs(inputs) for inputs in batch]
        )
        try:
            response = await stub.Infer(request, timeout=timeout_s)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # the gateway is stopping this dispatcher
            # The supervisor closed the channel: the worker exited during the batch.
            raise PolicyError(
                f"worker {self.name} exited during batch {batch_id}",
                reason=ReasonCode.WORKER_CRASHED,
            ) from None
        except grpc.aio.AioRpcError as exc:
            if exc.code() is grpc.StatusCode.DEADLINE_EXCEEDED and incarnation == self.incarnation:
                self._kill(f"did not finish batch {batch_id} in {timeout_s:.3f} s")
                raise PolicyError(
                    f"worker {self.name} hung", reason=ReasonCode.WORKER_TIMED_OUT
                ) from exc
            raise PolicyError(
                f"worker {self.name} unreachable: {exc.code().name}",
                reason=ReasonCode.WORKER_CRASHED,
            ) from exc
        if incarnation != self.incarnation or response.batch_id != batch_id:
            raise PolicyError(
                f"worker {self.name} restarted during batch {batch_id}",
                reason=ReasonCode.WORKER_CRASHED,
            )
        if response.HasField("error"):
            reason = response.error.reason
            try:
                code = ReasonCode(reason.code)
            except ValueError:
                code = ReasonCode.WORKER_INFERENCE_FAILED
            raise PolicyError(
                reason.message,
                out_of_memory=code is ReasonCode.WORKER_OUT_OF_MEMORY,
                reason=code,
            )
        self.peak_memory_mib = max(self.peak_memory_mib, int(response.peak_memory_mib))
        return outputs_from(response)

    def status(self) -> dict[str, Any]:
        process = self._process
        return {
            "backend": "workers",
            "state": self.state,
            "device": self.spec.device,
            "pid": process.pid if process is not None and process.returncode is None else None,
            "restarts": self.restarts,
            "peak_memory_mib": self.peak_memory_mib,
            "last_error": self.last_error,
            "worker": self.description,
        }
