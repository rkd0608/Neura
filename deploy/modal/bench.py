"""The GPU-sharing benchmark (design §9.5, §10, §16 step 0) on one NVIDIA L4 at Modal.

    uv run --no-project --with modal==1.6.1 modal run deploy/modal/bench.py [--size base]
        [--out bench-report]

Modal builds an image from this checkout with the models group's locked versions, runs
`neura-bench run --preset gpu` on an L4 once, and hands the report back to bench-report/
(README.md, results.json and bench.log). The container ends with the run, and Modal stops it
when the run outlasts NEURA_BENCH_HOURS (default 3) or this command is stopped. Needs a Modal
token: `modal token new`, or MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.

Costs: the L4 with 4 cores (8 vCPUs, as Google's g2-standard-8) and 32 GiB of memory (building
pi0 takes about 14 GB), about $1.25 an hour; a run takes about 1.5 hours. Modal's GPU hosts run
NVIDIA's 580 driver (CUDA 13), which the locked PyTorch is built for.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import modal

SRC = "/src"
# The checkout, here; in the container this file is /root/bench.py and the checkout is at SRC.
ROOT = Path(__file__).resolve().parents[2] if modal.is_local() else Path(SRC)
HOURS = float(os.environ.get("NEURA_BENCH_HOURS", "3"))
UV_VERSION = "0.11.32"
PYTHON = "3.13"
REPORT = ("README.md", "results.json")

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


@app.function(gpu="L4", cpu=4.0, memory=32 * 1024, timeout=int(HOURS * 3600), retries=0)
def bench(size: str) -> dict[str, bytes]:
    """Run the suite on this container's L4. Returns the report's files and the log."""
    smi = shutil.which("nvidia-smi")
    if smi:
        subprocess.run([smi], check=False)  # noqa: S603 - fixed arguments
    work = Path(tempfile.mkdtemp(prefix="neura-bench-"))
    out, log = work / "report", work / "bench.log"
    env = {**os.environ, "HF_HOME": str(work / "hf"), "MUJOCO_GL": "egl", "UV_PYTHON": PYTHON}
    command = [
        shutil.which("uv") or "uv", "run", "--frozen", "--group", "models", "neura-bench", "run",
        "--preset", "gpu", "--device", "cuda:0", "--size", size, "--out", str(out),
    ]  # fmt: skip
    # The output goes to the log and, through Modal, to whoever ran this.
    with (
        log.open("w") as sink,
        subprocess.Popen(  # noqa: S603 - fixed arguments but the size
            command, cwd=SRC, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        ) as process,
    ):
        for line in process.stdout or ():
            sink.write(line)
            print(line, end="", flush=True)
    files = {"bench.log": log.read_bytes()}
    if process.returncode == 0:
        files.update({name: (out / name).read_bytes() for name in REPORT})
    return files


@app.local_entrypoint()
def main(size: str = "base", out: str = "bench-report") -> None:
    files = bench.remote(size)
    dest = Path(out)
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (dest / name).write_bytes(data)
    if not all(name in files for name in REPORT):
        tail = files["bench.log"].decode(errors="replace").splitlines()[-80:]
        print("the benchmark did not finish; the end of its log:", *tail, sep="\n", file=sys.stderr)
        raise SystemExit(1)
    print(f"--> the report: {dest / 'README.md'}")
