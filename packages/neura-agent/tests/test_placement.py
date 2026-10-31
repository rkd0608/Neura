"""Measuring a model where it will run (design §13): the profiler runs the customer's code in the
worker sandbox, so it may write nothing at the site, and what it prints stays at the site."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from neura_agent import placement
from neura_agent.artifacts import ArtifactError
from neura_contracts import ReasonCode
from neura_server.sandbox import Invocation, ProcessLauncher, exit_status

MEASURED = {
    "points": [{"batch_size": 1, "p50_us": 1000, "p99_us": 2000, "memory_mib": 10}],
    "artifact_digest": "sha256:" + "a" * 64,
}


def documents(tmp_path: Path) -> dict[str, Path]:
    return {
        name: tmp_path / f"{name}.json"
        for name in ("model_artifact", "robot_profile", "execution_profile")
    }


async def measure(launcher: Any, tmp_path: Path, out: Path) -> dict[str, Any]:
    return await placement.measure(
        launcher,
        documents=documents(tmp_path),
        device="cpu",
        checkpoint=None,
        out=out,
        max_batch=1,
        iterations=1,
        timeout_s=30,
    )


class Profiler:
    """A profiler that prints a measured profile; the invocations it was given are kept."""

    def __init__(self) -> None:
        self.invocations: list[Invocation] = []

    def command(self, name: str, invocation: Invocation) -> list[str]:
        self.invocations.append(invocation)
        line = json.dumps({"profile": MEASURED})
        return [sys.executable, "-c", f"print({line!r})"]

    async def remove(self, name: str) -> None:
        del name


async def test_the_profiler_may_write_nothing_and_the_agent_keeps_its_profile(
    tmp_path: Path,
) -> None:
    # Every revision's measured profile is in one directory that admission and prepare read: a
    # profiler that could write there could rewrite another revision's.
    profiler = Profiler()
    out = tmp_path / "profiles" / "p.json"
    assert await measure(profiler, tmp_path, out) == MEASURED
    (invocation,) = profiler.invocations
    assert invocation.output_dir is None
    assert invocation.outputs == []
    assert "--out" not in invocation.host_args()
    assert json.loads(out.read_text()) == MEASURED


async def test_a_profiler_that_dies_says_how_and_keeps_its_output_at_the_site(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A worker that aborts before it can report: what it printed may quote the customer's code,
    # so it goes to the site's log and the reason only says how it ended.
    dies = ProcessLauncher(
        [sys.executable, "-c", "import os, sys; print('secret line', file=sys.stderr); os.abort()"]
    )
    out = tmp_path / "profile.json"
    with pytest.raises(ArtifactError) as raised:
        await measure(dies, tmp_path, out)
    assert raised.value.code is ReasonCode.WORKER_LOAD_FAILED
    assert "was killed by SIGABRT" in str(raised.value)
    assert "secret line" not in str(raised.value)
    assert "profiler: secret line" in caplog.text
    assert not out.exists()
    assert exit_status(3) == "exited with status 3"
