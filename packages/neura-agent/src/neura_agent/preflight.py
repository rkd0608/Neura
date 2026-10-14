"""Preflight checks run before enrollment and reported continuously (design §17.2).

Each check says what passed or failed and, when it fails, how to fix it.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from neura.v1 import site_pb2
from neura_contracts import ReasonCode

NVIDIA_SMI_QUERY = [
    "nvidia-smi",
    "--query-gpu=index,uuid,name,memory.total,mig.mode.current,driver_version",
    "--format=csv,noheader,nounits",
]

Runner = Callable[[list[str]], str | None]


def run_command(args: list[str]) -> str | None:
    if shutil.which(args[0]) is None:
        return None
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


@dataclass(frozen=True, slots=True)
class Gpu:
    index: int
    uuid: str
    model: str
    memory_mib: int
    mig_enabled: bool
    driver_version: str


def parse_nvidia_smi(output: str) -> list[Gpu]:
    gpus = []
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            gpus.append(
                Gpu(
                    index=int(parts[0]),
                    uuid=parts[1],
                    model=parts[2],
                    memory_mib=int(float(parts[3])),
                    mig_enabled=parts[4].lower() == "enabled",
                    driver_version=parts[5],
                )
            )
        except ValueError:
            continue
    return gpus


def cuda_version(runner: Runner) -> str:
    output = runner(["nvidia-smi"]) or ""
    marker = "CUDA Version:"
    if marker in output:
        return output.split(marker, 1)[1].split()[0].strip(" |")
    return ""


def collect_facts(
    *,
    state_dir: Path,
    require_gpu: bool,
    min_free_disk_mib: int,
    allowed_gpu_indexes: list[int] | None,
    runner: Runner = run_command,
) -> site_pb2.ServerFacts:
    checks: list[site_pb2.PreflightResult] = []

    def check(
        name: str, passed: bool, detail: str, fix: str = "", reason: ReasonCode | None = None
    ) -> None:
        checks.append(
            site_pb2.PreflightResult(
                name=name,
                passed=passed,
                reason="" if passed else (reason or ReasonCode.AGENT_PREFLIGHT_FAILED).value,
                detail=detail,
                fix="" if passed else fix,
            )
        )

    smi = runner(NVIDIA_SMI_QUERY)
    gpus = parse_nvidia_smi(smi) if smi else []
    if gpus:
        check("nvidia_driver", True, f"driver {gpus[0].driver_version}, {len(gpus)} GPU(s)")
    else:
        check(
            "nvidia_driver",
            not require_gpu,
            "nvidia-smi not found or reported no GPUs"
            + ("" if require_gpu else " (not required by this agent's configuration)"),
            "Install the NVIDIA driver and make nvidia-smi available on PATH.",
        )
    if allowed_gpu_indexes is not None:
        known = {g.index for g in gpus}
        missing = sorted(set(allowed_gpu_indexes) - known)
        check(
            "allowed_gpus_present",
            not missing,
            f"allowed GPU indexes {allowed_gpu_indexes}; present {sorted(known)}",
            f"Remove {missing} from NEURA_AGENT_ALLOWED_GPU_INDEXES or check the hardware.",
        )

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".write-test"
        probe.write_bytes(b"ok")
        probe.unlink()
        writable = True
    except OSError:
        writable = False
    check(
        "state_dir_writable",
        writable,
        str(state_dir),
        f"Create {state_dir} and make it writable by the agent's user.",
    )
    if writable:
        free_mib = shutil.disk_usage(state_dir).free // (1024 * 1024)
        check(
            "free_disk",
            free_mib >= min_free_disk_mib,
            f"{free_mib} MiB free (need {min_free_disk_mib})",
            "Free disk space on the volume holding the agent state directory.",
        )
    check(
        "cpu_count",
        (os.cpu_count() or 0) >= 2,
        f"{os.cpu_count()} CPUs",
        "Use a server with at least 2 CPUs.",
    )

    return site_pb2.ServerFacts(
        hostname=socket.gethostname(),
        driver_version=gpus[0].driver_version if gpus else "",
        cuda_version=cuda_version(runner) if gpus else "",
        gpus=[
            site_pb2.GpuInfo(
                index=g.index,
                uuid=g.uuid,
                model=g.model,
                memory_mib=g.memory_mib,
                mig_enabled=g.mig_enabled,
            )
            for g in gpus
        ],
        preflight=checks,
    )
