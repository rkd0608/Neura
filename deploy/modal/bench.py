"""The GPU-sharing benchmark (design §9.5, §10, §16 step 0) on one NVIDIA L4 at Modal.

    uv run --no-project --with modal==1.6.1 modal run deploy/modal/bench.py [--preset gpu|vla]
        [--size base] [--out bench-report]

Modal builds an image from this checkout with the models group's locked versions and, on an
L4, runs every stage of the suite small first (`--preset smoke-gpu`, a few minutes: the code
paths a CPU never takes, and pi0), then `neura-bench run --preset gpu` (or `vla`) once, and
hands the report back to bench-report/ (README.md, results.json and bench.log). A failed first
stage ends the run before the long one. The container ends with the run, and Modal stops it when
the run outlasts NEURA_BENCH_HOURS (default 3) or this command is stopped. Ten minutes before
that limit the run stops itself and hands back the report it wrote before its real-time runs,
so running out of time costs those runs, not the measurements and simulations. Needs a Modal
token: `modal token new`, or MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.

Costs: the L4 with 4 cores (8 vCPUs, as Google's g2-standard-8) and 32 GiB of memory (building
pi0 takes about 14 GB; NEURA_BENCH_MEMORY_GIB=64 for the vla preset's 24 co-located workers),
about $1.25 an hour; the gpu preset takes about 2 hours 40 minutes on Modal. Modal's GPU hosts
run NVIDIA's 580 driver (CUDA 13), which the locked PyTorch is built for.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TextIO

import modal

SRC = "/src"
# The checkout, here; in the container this file is /root/bench.py and the checkout is at SRC.
ROOT = Path(__file__).resolve().parents[2] if modal.is_local() else Path(SRC)
HOURS = float(os.environ.get("NEURA_BENCH_HOURS", "3"))
MEMORY_GIB = int(os.environ.get("NEURA_BENCH_MEMORY_GIB", "32"))
UV_VERSION = "0.11.32"
PYTHON = "3.13"
REPORT = ("README.md", "results.json")
# Time kept back from Modal's limit to stop the run and hand its report back.
MARGIN_S = 600

image = (
    modal.Image.debian_slim(python_version=PYTHON)
    # MuJoCo's and OpenCV's shared libraries, as on the Deep Learning VM.
    .apt_install("libegl1", "libgl1", "libglib2.0-0")
    .pip_install(f"uv=={UV_VERSION}")
    .add_local_dir(
        ROOT,
        SRC,
        copy=True,
        ignore=[".git", "**/node_modules", "**/.venv", "**/__pycache__", "bench-report"],
    )
    .run_commands(f"cd {SRC} && UV_PYTHON={PYTHON} uv sync --frozen --group models")
)
app = modal.App("neura-bench", image=image)


def _run(arguments: list[str], cwd: str, env: dict[str, str], sink: TextIO, seconds: float) -> int:
    """``neura-bench run`` with these arguments, stopped after ``seconds``; its output to the
    log and, through Modal, to whoever ran this."""
    command = [shutil.which("uv") or "uv", "run", "--frozen", "--group", "models", "neura-bench"]
    with subprocess.Popen(  # noqa: S603 - fixed arguments but the size
        [*command, "run", *arguments],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        timer = threading.Timer(max(seconds, 0.0), process.terminate)
        timer.start()
        try:
            for line in process.stdout or ():
                sink.write(line)
                print(line, end="", flush=True)
        finally:
            timer.cancel()
    return process.returncode


@app.function(gpu="L4", cpu=4.0, memory=MEMORY_GIB * 1024, timeout=int(HOURS * 3600), retries=0)
def bench(
    size: str, smoke: bool = True, hours: float = HOURS, commit: str = "", preset: str = "gpu"
) -> dict[str, bytes]:
    """Run the suite on this container's L4. Returns the report's files, the log and the run's
    exit code."""
    deadline = time.monotonic() + hours * 3600 - MARGIN_S
    smi = shutil.which("nvidia-smi")
    if smi:
        subprocess.run([smi], check=False)  # noqa: S603 - fixed arguments
    work = Path(tempfile.mkdtemp(prefix="neura-bench-"))
    out, log = work / "report", work / "bench.log"
    env = {**os.environ, "HF_HOME": str(work / "hf"), "MUJOCO_GL": "egl", "UV_PYTHON": PYTHON}
    env["NEURA_GIT_COMMIT"] = commit  # the image holds the checkout without its history
    with log.open("w") as sink:
        if smoke:
            print("--> every stage small, on the GPU", flush=True)
            arguments = ["--preset", "smoke-gpu", "--out", str(work / "smoke")]
            if _run(arguments, SRC, env, sink, deadline - time.monotonic()):
                print("--> the small run failed: no long run", flush=True)
                return {"bench.log": log.read_bytes(), "exit": b"1"}
        print("--> the benchmark", flush=True)
        arguments = ["--preset", preset, "--device", "cuda:0", "--size", size, "--out", str(out)]
        code = _run(arguments, SRC, env, sink, deadline - time.monotonic())
        if time.monotonic() >= deadline:
            print("--> stopped for time: the report written before the real-time runs", flush=True)
    files = {"bench.log": log.read_bytes(), "exit": str(code).encode()}
    # A run that stopped early keeps the report it wrote after its simulations.
    files.update({name: (out / name).read_bytes() for name in REPORT if (out / name).exists()})
    return files


@app.local_entrypoint()
def main(
    size: str = "base", out: str = "bench-report", smoke: bool = True, preset: str = "gpu"
) -> None:
    if preset not in ("gpu", "vla"):
        raise SystemExit(f"--preset is gpu or vla, not {preset!r}")
    git = shutil.which("git")
    commit = ""
    if git:
        head = [git, "-C", str(ROOT), "rev-parse", "--short", "HEAD"]
        commit = subprocess.run(head, capture_output=True, text=True, check=False).stdout.strip()  # noqa: S603
    files = bench.remote(size, smoke, HOURS, commit, preset)
    code = int(files.pop("exit", b"1"))
    dest = Path(out)
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (dest / name).write_bytes(data)
    tail = files["bench.log"].decode(errors="replace").splitlines()[-80:]
    if not all(name in files for name in REPORT):
        print("the benchmark did not finish; the end of its log:", *tail, sep="\n", file=sys.stderr)
        raise SystemExit(1)
    if code != 0:
        # Kept and committed: the report says which real-time runs it lacks.
        print(f"the benchmark stopped early (exit {code}); the report has what finished:")
        print(*tail[-20:], sep="\n")
    print(f"--> the report: {dest / 'README.md'}")
