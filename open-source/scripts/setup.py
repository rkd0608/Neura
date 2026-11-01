"""Install neura-robot with every extra this machine can run, and the right PyTorch for it.

    python scripts/setup.py                # or: make setup-all
    python scripts/setup.py --torch cpu    # or: make setup-all TORCH=cpu
    python scripts/setup.py --dry-run      # print what it would run

LeRobot (the SO-101 driver, its server, recorded robots) brings PyTorch, and which PyTorch
matters:

- Linux: PyPI's PyTorch always installs the CUDA libraries, several GB, GPU or not. Without an
  NVIDIA GPU (no ``nvidia-smi``) it installs the CPU build instead, from PyTorch's own index,
  in the same step, so LeRobot's version limits still hold.
- macOS on Apple Silicon: PyPI's PyTorch has no CUDA; it runs on the CPU and the GPU (Metal).
- macOS on Intel: PyTorch publishes no builds newer than 2.2, and LeRobot needs 2.7, so
  everything but the LeRobot extras is installed.
- Python older than 3.12: LeRobot needs 3.12, so likewise.

``--torch cpu`` or ``--torch cuda`` overrides the choice on Linux.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys

CPU_INDEX = "https://download.pytorch.org/whl/cpu"
ALWAYS = ["dev", "openpi", "sim"]
LEROBOT = ["so101", "lerobot"]


def plan(torch: str) -> tuple[list[str], list[str], list[str]]:
    """The extras, the extra pip arguments, and what to tell the person."""
    notes = []
    extras = list(ALWAYS)
    system, machine = platform.system(), platform.machine().lower()
    if sys.version_info < (3, 12):
        notes.append(
            f"Python {sys.version_info.major}.{sys.version_info.minor}: LeRobot needs 3.12 or "
            "newer, so the SO-101 driver, the LeRobot server and recorded robots are left out "
            "(make the environment with python3.12 for them)"
        )
    elif system == "Darwin" and machine in ("x86_64", "amd64"):
        notes.append(
            "an Intel Mac: PyTorch publishes no build newer than 2.2 for it and LeRobot needs "
            "2.7, so the SO-101 driver, the LeRobot server and recorded robots are left out"
        )
    else:
        extras += LEROBOT
    pip: list[str] = []
    if any(e in extras for e in LEROBOT) and system == "Linux":
        if torch == "auto":
            torch = "cuda" if shutil.which("nvidia-smi") else "cpu"
            why = "an NVIDIA GPU" if torch == "cuda" else "no NVIDIA GPU (no nvidia-smi)"
            notes.append(f"PyTorch: the {torch.upper()} build, for {why}; --torch overrides it")
        if torch == "cpu":
            pip = ["--extra-index-url", CPU_INDEX]
    elif any(e in extras for e in LEROBOT) and system == "Darwin":
        notes.append("PyTorch: PyPI's build, which uses the CPU and Apple's GPU (Metal)")
    return extras, pip, notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--torch", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--dry-run", action="store_true", help="print the command, run nothing")
    args = parser.parse_args()
    extras, pip, notes = plan(args.torch)
    command = [sys.executable, "-m", "pip", "install", "-e", f".[{','.join(extras)}]", *pip]
    for note in notes:
        print(f"- {note}")
    print("$ " + " ".join(command), flush=True)
    if args.dry_run:
        return 0
    done = subprocess.run(command, check=False)  # noqa: S603 - our own fixed command
    if done.returncode != 0:
        return done.returncode
    if any(e in extras for e in LEROBOT):
        check = (
            "import torch; print('PyTorch', torch.__version__,"
            " '- CUDA:', torch.cuda.is_available(), '- Metal:', torch.backends.mps.is_available())"
        )
        subprocess.run([sys.executable, "-c", check], check=False)  # noqa: S603
    print("Installed. Next: make doctor, then make check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
