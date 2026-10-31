"""The policy worker process: one model on one device, serving one batch group (design §9.1, §12).

``neura-worker serve`` loads the artifact's adapter, verifies its checkpoint, warms it up and then
serves `neura.v1.PolicyWorker` on a Unix domain socket. Its first line on stdout says whether it
is ready (``{"ready": {...}}``) or why it could not start (``{"error": {"code", "message"}}``);
logs go to stderr. It watches its stdin from the moment it starts: when the gateway closes it, or
exits, a worker still loading exits at once and one serving stops its server, so a worker never
outlives its gateway.

``neura-worker profile`` loads and warms up the same way, measures latency per batch size and
peak memory on the device, and prints the profile as JSON. The site agent runs it at prepare, so
capacity estimates and admission use measured numbers (design §13).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import resource
import stat
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, cast

import grpc
import numpy as np
from numpy.typing import NDArray

from neura.v1 import common_pb2, worker_pb2, worker_pb2_grpc
from neura_adapters import Adapter, load_adapter
from neura_adapters.profiler import profile_adapter
from neura_contracts import ReasonCode
from neura_runtime import tensors
from neura_runtime.binding import ExecutionBinding
from neura_runtime.policy import ModelInputs, PolicyError
from neura_server.config import deployment_device, load_binding_files

log = logging.getLogger("neura.worker")
MAX_MESSAGE_BYTES = 512 * 1024 * 1024
SUPPORTED_PRECISIONS = ("fp32", "fp16", "bf16")


class LoadError(Exception):
    def __init__(self, code: ReasonCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def verify_checkpoint(binding: ExecutionBinding, checkpoint: Path) -> None:
    """Every weight file the artifact declares is present with its declared size. Digests are
    verified when the site agent imports the files; the checkout is read-only."""
    problems = []
    for weight in binding.artifact.spec.weights:
        path = checkpoint / weight.path
        if not path.is_file():
            problems.append(f"{weight.path} is missing")
        elif path.stat().st_size != weight.size_bytes:
            problems.append(
                f"{weight.path} has {path.stat().st_size} bytes, the artifact declares "
                f"{weight.size_bytes}"
            )
    if problems:
        raise LoadError(
            ReasonCode.AGENT_ARTIFACT_UNAVAILABLE,
            f"checkpoint {checkpoint} does not hold the artifact's weights: " + "; ".join(problems),
        )


def load(args: argparse.Namespace) -> tuple[Adapter, ExecutionBinding]:
    try:
        binding = load_binding_files(args.artifact, args.robot_profile, args.execution_profile)
    except (OSError, ValueError) as exc:
        raise LoadError(ReasonCode.CONTRACT_INVALID, f"cannot load the contracts: {exc}") from exc
    name = args.adapter or binding.artifact.spec.adapter.name
    precision = binding.profile.spec.runtime.precision.value
    if precision not in SUPPORTED_PRECISIONS:
        raise LoadError(
            ReasonCode.WORKER_LOAD_FAILED, f"precision {precision} is not supported by workers yet"
        )
    device = deployment_device(binding, args.device)
    checkpoint: Path | None = args.checkpoint
    if checkpoint is not None:
        verify_checkpoint(binding, checkpoint)
    try:
        adapter = load_adapter(
            name,
            binding,
            checkpoint=checkpoint,
            device=device,
            precision=cast(Literal["fp32", "fp16", "bf16"], precision),
            seed=args.seed,
        )
        adapter.warmup(sorted({1, *args.warmup}))
    except PolicyError as exc:
        code = exc.reason
        if code is ReasonCode.WORKER_INFERENCE_FAILED:  # the default: nothing more specific
            code = ReasonCode.WORKER_LOAD_FAILED
        raise LoadError(code, str(exc)) from exc
    except MemoryError as exc:
        raise LoadError(ReasonCode.WORKER_OUT_OF_MEMORY, str(exc)) from exc
    except Exception as exc:
        log.exception("loading %s failed", name)
        raise LoadError(ReasonCode.WORKER_LOAD_FAILED, f"{type(exc).__name__}: {exc}") from exc
    return adapter, binding


def describe(adapter: Adapter, binding: ExecutionBinding) -> worker_pb2.WorkerDescription:
    properties = adapter.properties
    return worker_pb2.WorkerDescription(
        artifact_digest=binding.artifact_digest,
        adapter=properties.name,
        device=properties.device,
        precision=properties.precision,
        deterministic=properties.deterministic,
        batching=properties.batching,
        multi_adapter=properties.multi_adapter,
        parameters=properties.parameters or 0,
        chunk_length=adapter.chunk_length,
        action_dim=adapter.action_dim,
        pid=os.getpid(),
    )


# --- wire format ---------------------------------------------------------------------------


def encode_inputs(inputs: ModelInputs) -> worker_pb2.WorkerInputs:
    message = worker_pb2.WorkerInputs(instruction=inputs.instruction)
    for name, vector in inputs.state.items():
        message.state[name].CopyFrom(tensors.encode(vector))
    for role, image in inputs.images.items():
        message.images[role].CopyFrom(tensors.encode(image))
    if inputs.frozen_prefix is not None:
        message.frozen_prefix.CopyFrom(tensors.encode(inputs.frozen_prefix))
    return message


def decode_inputs(message: worker_pb2.WorkerInputs) -> ModelInputs:
    state = {
        name: np.asarray(tensors.decode(t), dtype=np.float32) for name, t in message.state.items()
    }
    images = {
        role: np.asarray(tensors.decode(t), dtype=np.uint8) for role, t in message.images.items()
    }
    prefix = (
        np.asarray(tensors.decode(message.frozen_prefix), dtype=np.float32)
        if message.HasField("frozen_prefix")
        else None
    )
    return ModelInputs(
        state=state, images=images, instruction=message.instruction, frozen_prefix=prefix
    )


# --- serving -------------------------------------------------------------------------------


class _Servicer(worker_pb2_grpc.PolicyWorkerServicer):  # type: ignore[misc]
    def __init__(self, adapter: Adapter, binding: ExecutionBinding) -> None:
        self._adapter = adapter
        self._description = describe(adapter, binding)
        self._lock = asyncio.Lock()
        self.batches = 0

    async def Describe(  # noqa: N802 - gRPC method name
        self, request: worker_pb2.DescribeRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> worker_pb2.WorkerDescription:
        return self._description

    async def Infer(  # noqa: N802
        self, request: worker_pb2.InferRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> worker_pb2.InferResponse:
        def error(code: ReasonCode, message: str) -> worker_pb2.InferResponse:
            return worker_pb2.InferResponse(
                batch_id=request.batch_id,
                error=worker_pb2.WorkerError(
                    reason=common_pb2.Reason(code=code.value, message=message[:1000])
                ),
            )

        try:
            batch = [decode_inputs(m) for m in request.inputs]
        except tensors.TensorError as exc:
            return error(ReasonCode.PROTOCOL_MESSAGE_INVALID, str(exc))
        async with self._lock:  # one forward pass at a time
            started = time.perf_counter_ns()
            try:
                outputs = await asyncio.to_thread(self._adapter.infer, batch)
            except PolicyError as exc:
                if exc.out_of_memory:
                    _release_cached_memory()
                return error(exc.reason, str(exc))
            except MemoryError as exc:
                return error(ReasonCode.WORKER_OUT_OF_MEMORY, str(exc))
            except Exception as exc:
                log.exception("inference failed")
                return error(ReasonCode.WORKER_INFERENCE_FAILED, f"{type(exc).__name__}: {exc}")
            compute_us = (time.perf_counter_ns() - started) // 1000
        self.batches += 1
        chunks = worker_pb2.Chunks(
            chunks=[tensors.encode(np.ascontiguousarray(o, dtype=np.float32)) for o in outputs]
        )
        return worker_pb2.InferResponse(
            batch_id=request.batch_id,
            chunks=chunks,
            compute_us=compute_us,
            peak_memory_mib=peak_memory_mib(self._adapter.properties.device),
        )


def peak_memory_mib(device: str) -> int:
    """Device memory the model's tensors used at most (CUDA), or the process's peak resident
    memory (CPU, an upper bound that includes the runtime)."""
    torch = sys.modules.get("torch")
    if device.startswith("cuda") and torch is not None:
        return int(torch.cuda.max_memory_allocated(torch.device(device)) // (1 << 20))
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)


def _release_cached_memory() -> None:
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


class _Gateway:
    """The gateway that started this worker, watched on stdin from the moment the worker starts:
    it closes stdin, or exits, to stop the worker. While the worker loads (which can take
    minutes), it then exits at once: there is nothing of its to finish. Once it serves, it stops
    its server first. A worker whose stdin is not a pipe was not started by a gateway (a person
    or a test runs it): it only stops serving."""

    def __init__(self) -> None:
        try:
            mode = os.fstat(0).st_mode
        except OSError:
            mode = 0
        self._piped = stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)
        self._lock = threading.Lock()
        self._gone = False
        self._stop: Callable[[], None] | None = None
        threading.Thread(target=self._watch, name="gateway", daemon=True).start()

    def _watch(self) -> None:
        with contextlib.suppress(OSError):
            while os.read(0, 4096):
                pass
        with self._lock:
            self._gone = True
            stop = self._stop
        if stop is not None:
            stop()
        elif self._piped:
            os._exit(0)  # still loading

    def on_gone(self, stop: Callable[[], None]) -> None:
        """Call ``stop`` (from the watching thread) when the gateway goes; at once if it has."""
        with self._lock:
            self._stop = stop
            gone = self._gone
        if gone:
            stop()


async def serve(
    adapter: Adapter, binding: ExecutionBinding, socket: Path, gateway: _Gateway
) -> None:
    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ]
    )
    servicer = _Servicer(adapter, binding)
    worker_pb2_grpc.add_PolicyWorkerServicer_to_server(servicer, server)
    with contextlib.suppress(FileNotFoundError):
        socket.unlink()
    server.add_insecure_port(f"unix://{socket}")
    await server.start()
    os.chmod(socket, 0o600)
    loop = asyncio.get_running_loop()
    gone = asyncio.Event()

    def stop() -> None:
        with contextlib.suppress(RuntimeError):  # the loop has closed: serving is over anyway
            loop.call_soon_threadsafe(gone.set)

    gateway.on_gone(stop)
    _announce({"ready": _description_json(servicer)})
    try:
        await gone.wait()
        log.info("gateway went away; stopping after %d batches", servicer.batches)
    finally:
        await server.stop(grace=1.0)
        with contextlib.suppress(FileNotFoundError):
            socket.unlink()


def _description_json(servicer: _Servicer) -> dict[str, Any]:
    d = servicer._description
    return {
        "artifact_digest": d.artifact_digest,
        "adapter": d.adapter,
        "device": d.device,
        "precision": d.precision,
        "deterministic": d.deterministic,
        "parameters": d.parameters,
        "pid": d.pid,
    }


def _announce(line: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    sys.stdout.flush()


def _sizes(text: str) -> list[int]:
    sizes = sorted({int(s) for s in text.split(",") if s.strip()})
    if not sizes or sizes[0] < 1:
        raise argparse.ArgumentTypeError("batch sizes must be positive integers")
    return sizes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neura-worker", description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("serve", "profile"):
        command = sub.add_parser(mode)
        command.add_argument("--artifact", type=Path, required=True)
        command.add_argument("--robot-profile", type=Path, required=True)
        command.add_argument("--execution-profile", type=Path, required=True)
        command.add_argument("--checkpoint", type=Path, default=None)
        command.add_argument("--adapter", default=None, help="override the artifact's adapter")
        command.add_argument("--device", default=None, help="cpu or cuda:N")
        command.add_argument("--seed", type=int, default=0)
        command.add_argument("--warmup", type=_sizes, default=[1], help="batch sizes, e.g. 1,4,8")
        command.add_argument("--log-level", default=os.environ.get("NEURA_LOG_LEVEL", "info"))
        if mode == "serve":
            command.add_argument("--socket", type=Path, required=True)
        else:
            command.add_argument("--batch-sizes", type=_sizes, default=[1, 2, 4, 8])
            command.add_argument("--iterations", type=int, default=30)
            command.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    gateway = _Gateway() if args.mode == "serve" else None  # profiling has no gateway
    try:
        adapter, binding = load(args)
    except LoadError as exc:
        _announce({"error": {"code": exc.code.value, "message": str(exc)}})
        return 3
    if args.mode == "profile":
        try:
            measured = profile_adapter(
                adapter, batch_sizes=args.batch_sizes, iterations=args.iterations
            )
        except PolicyError as exc:
            code = ReasonCode.WORKER_OUT_OF_MEMORY if exc.out_of_memory else exc.reason
            _announce({"error": {"code": code.value, "message": str(exc)}})
            return 3
        result = {**measured.to_json(), "artifact_digest": binding.artifact_digest}
        if args.out is not None:
            args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        _announce({"profile": result})
        return 0
    assert gateway is not None
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(adapter, binding, args.socket, gateway))
    return 0


def outputs_from(response: worker_pb2.InferResponse) -> list[NDArray[np.float32]]:
    """The chunks of a successful response (gateway side)."""
    return [
        np.asarray(tensors.decode(t, require_finite=False), dtype=np.float32)
        for t in response.chunks.chunks
    ]


if __name__ == "__main__":
    sys.exit(main())
