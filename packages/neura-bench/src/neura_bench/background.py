"""A non-policy compute job for the "concurrent GPU job" workload (design §10): matrix
multiplications back to back on the device, until killed or ``--seconds`` pass."""

from __future__ import annotations

import argparse
import time

import numpy as np


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m neura_bench.background")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seconds", type=float, default=3600)
    parser.add_argument("--size", type=int, default=0, help="matrix size (default by device)")
    args = parser.parse_args(argv)
    deadline = time.monotonic() + args.seconds
    if args.device.startswith("cuda"):
        import torch

        size = args.size or 8192
        a = torch.randn(size, size, device=args.device, dtype=torch.float16)
        b = torch.randn(size, size, device=args.device, dtype=torch.float16)
        while time.monotonic() < deadline:
            for _ in range(10):
                a @ b
            torch.cuda.synchronize()
    else:
        size = args.size or 1024
        rng = np.random.default_rng(0)
        x = rng.standard_normal((size, size), dtype=np.float32)
        y = rng.standard_normal((size, size), dtype=np.float32)
        while time.monotonic() < deadline:
            x @ y


if __name__ == "__main__":
    main()
