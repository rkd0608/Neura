"""What runs a batch group's forward passes, as seen from the gateway's dispatcher.

`WorkerBackend` supervises one policy worker process (`neura_server.worker`) per batch group and
talks to it over a Unix socket (design §9.1, §12). It restarts a worker that exits or hangs, and
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
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import grpc
import numpy as np
from numpy.typing import NDArray

from neura.v1 import worker_pb2, worker_pb2_grpc
from neura_adapters import ModelInputs, PolicyBackend, PolicyError
from neura_contracts import ReasonCode
from neura_server.worker import MAX_MESSAGE_BYTES, encode_inputs, outputs_from

log = logging.getLogger("neura.gateway.workers")
HEALTHY_RUN_S = 30.0
MAX_BACKOFF_S = 30.0
UNIX_PATH_MAX = 107


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

    def arguments(self, socket: Path) -> list[str]:
        args = [
            "serve",
            "--socket",
            str(socket),
            "--artifact",
            str(self.artifact),
            "--robot-profile",
            str(self.robot_profile),
            "--execution-profile",
            str(self.execution_profile),
            "--device",
            self.device,
            "--warmup",
            ",".join(str(b) for b in self.warmup_batches),
        ]
        if self.checkpoint is not None:
            args += ["--checkpoint", str(self.checkpoint)]
        if self.adapter is not None:
            args += ["--adapter", self.adapter]
        return args


class WorkerBackend:
    def __init__(
        self,
        name: str,
        spec: WorkerSpec,
        *,
        command: Sequence[str],
        socket_dir: Path,
        start_timeout_s: float,
    ) -> None:
        self.name = name
        self.spec = spec
        self._command = list(command)
        self._socket_dir = socket_dir
        self._start_timeout_s = start_timeout_s
        if len(str(self._socket_path(999_999))) > UNIX_PATH_MAX:
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
        self._stopping = False
        self.ready = asyncio.Event()

    def _socket_path(self, incarnation: int) -> Path:
        return self._socket_dir / f"{self.name}-{incarnation}.sock"

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
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        await self._disconnect()
        self.state = "stopped"

    def _stop_requested(self) -> bool:
        return self._stopping

    async def _supervise(self) -> None:
        backoff = 0.5
        while not self._stop_requested():
            self.incarnation += 1
            socket = self._socket_path(self.incarnation)
            self.state = "starting" if self.restarts == 0 else "restarting"
            started = time.monotonic()
            process = self._process = await asyncio.create_subprocess_exec(
                *self._command,
                *self.spec.arguments(socket),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            log.info("worker %s starting (pid %s) on %s", self.name, process.pid, self.spec.device)
            drain: asyncio.Task[None] | None = None
            try:
                announced = await self._await_announcement(process)
                if "ready" in announced:
                    await self._connect(socket)
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
            if time.monotonic() - started > HEALTHY_RUN_S:
                backoff = 0.5
            log.warning("worker %s exited with %s; restarting in %.1fs", self.name, code, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

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

    async def _connect(self, socket: Path) -> None:
        self._channel = grpc.aio.insecure_channel(
            f"unix://{socket}",
            options=[
                ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
                ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
            ],
        )
        self._stub = worker_pb2_grpc.PolicyWorkerStub(self._channel)
        await self._stub.Describe(worker_pb2.DescribeRequest(), timeout=10)

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
