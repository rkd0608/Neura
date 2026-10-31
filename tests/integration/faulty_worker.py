"""A policy worker whose mock policy hangs, exits or fails on demand (tests only).

Run as the gateway's worker command. The fault is read from the file named by
NEURA_TEST_WORKER_FAULT before every batch: "hang", "exit" (once) or "error"; and before loading:
"slow-load" takes a minute to load, after writing the worker's pid to that file + ".loading".
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_adapters import ModelInputs, base
from neura_adapters.mock import MockPolicy
from neura_runtime.binding import ExecutionBinding
from neura_server import worker

FAULT = Path(os.environ["NEURA_TEST_WORKER_FAULT"])


class FaultyPolicy(MockPolicy):
    def infer(self, batch: Sequence[ModelInputs]) -> list[NDArray[np.float32]]:
        fault = FAULT.read_text().strip() if FAULT.exists() else ""
        if fault == "hang":
            time.sleep(60)
        elif fault == "exit":
            FAULT.write_text("")
            os._exit(17)
        elif fault == "error":
            raise RuntimeError("injected failure")
        return super().infer(batch)


def load(*, binding: ExecutionBinding, **_: Any) -> FaultyPolicy:
    if FAULT.exists() and FAULT.read_text().strip() == "slow-load":
        FAULT.with_suffix(".loading").write_text(str(os.getpid()))
        time.sleep(60)
    return FaultyPolicy(binding)


base._REGISTRY["faulty"] = f"{__name__}:load"

if __name__ == "__main__":
    sys.exit(worker.main())
