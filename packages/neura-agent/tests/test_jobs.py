"""Site jobs as the agent runs them (design §28.3, §28.4): a job runs the customer's model code in
the worker sandbox, so it may write only its own output."""

from __future__ import annotations

import asyncio
import contextlib
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from test_core import Site

from neura_agent import jobs
from neura_agent.runner import AgentRuntime
from neura_contracts import ReasonCode
from neura_server.config import SandboxConfig
from neura_server.sandbox import ContainerLauncher, Invocation, Launcher, launcher_for

# A job that makes its output and writes its report there, as neura-learn does, saying how the
# directory around it looked.
WRITES_ITS_REPORT = """
import json, os, stat, sys
out = sys.argv[sys.argv.index("--out") + 1]
os.mkdir(out)
mode = stat.S_IMODE(os.stat(os.path.dirname(out)).st_mode)
with open(os.path.join(out, "report.json"), "w") as report:
    json.dump({"out_mode": mode}, report)
"""


class Recorded:
    """The launcher the agent would use, with every invocation it was given kept."""

    def __init__(self, launcher: Launcher) -> None:
        self.launcher = launcher
        self.invocations: list[Invocation] = []

    def command(self, name: str, invocation: Invocation) -> list[str]:
        self.invocations.append(invocation)
        return self.launcher.command(name, invocation)

    async def remove(self, name: str) -> None:
        await self.launcher.remove(name)


def site(tmp_path: Path, script: str) -> Site:
    return Site(tmp_path, job_command=[sys.executable, "-c", script], job_device="cpu")


async def run(site: Site, binding: Any, inputs: list[tuple[str, Path]]) -> dict[str, Any]:
    work = site.core.jobs._workdir("job-1")
    model = jobs._Model("rev_1", {}, binding, work)
    return await site.core.jobs._run(
        "cmd_1", "opr_1", "evaluate", {"device": "cpu"}, inputs, work, model
    )


async def test_a_job_may_write_only_its_own_output(
    tmp_path: Path, make_binding: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Its episodes are hard links into the site's episode store, and its spec says what it runs:
    # both are inputs, read-only.
    recorded: list[Recorded] = []

    def recording(*args: Any) -> Recorded:
        recorded.append(Recorded(launcher_for(*args)))
        return recorded[-1]

    monkeypatch.setattr(jobs, "launcher_for", recording)
    s = site(tmp_path, WRITES_ITS_REPORT)
    episodes = tmp_path / "episodes"
    episodes.mkdir()
    result = await run(s, make_binding(), [("--episodes", episodes)])
    assert result == {"out_mode": 0o700}
    ((invocation,),) = [r.invocations for r in recorded]
    work = s.core.jobs.directory / "job-1"
    assert invocation.output_dir == work / "out"
    assert invocation.outputs == [("--out", "result")]
    assert [flag for flag, _ in invocation.inputs] == ["--spec", "--episodes"]
    # In the container, that directory is the only one it may write.
    command = ContainerLauncher(SandboxConfig(kind="container"), "image").command("j", invocation)
    volumes = [command[i + 1] for i, a in enumerate(command) if a == "--volume"]
    assert [v for v in volumes if v.endswith(":rw")] == [
        f"{(work / 'out').resolve()}:/neura/out:rw"
    ]
    assert stat.S_IMODE((work / "out").stat().st_mode) == 0o700
    assert json.loads((work / "out" / "result" / "report.json").read_text()) == result


# A job that fails, saying why in its own words: the customer's code and data can be in them.
FAILS_SAYING_WHY = """
import json, sys
print(json.dumps({"event": "failed", "error": "ValueError: row 7 of customer-data-4471"}))
print("Traceback: customer_model.py, line 3: secret-weights-path", file=sys.stderr)
sys.exit(1)
"""


async def test_a_failed_job_says_how_it_ended_and_keeps_what_it_said_at_the_site(
    tmp_path: Path, make_binding: Any, caplog: pytest.LogCaptureFixture
) -> None:
    s = site(tmp_path, FAILS_SAYING_WHY)

    async def work() -> None:
        await run(s, make_binding(), [])

    s.core.jobs._start("cmd_1", "opr_1", work)
    await s.core.jobs._tasks["opr_1"]
    sent = s.drain()
    (failed,) = [m.operation for m in sent if m.operation.state == "Failed"]
    assert failed.reason.code == ReasonCode.AGENT_JOB_FAILED.value
    assert (
        failed.message
        == "the evaluate job exited with status 1 (its output is in the site agent's log)"
    )
    for marker in (b"customer-data-4471", b"secret-weights-path"):
        assert not any(marker in m.SerializeToString() for m in sent)
        assert marker.decode() in caplog.text


async def started(s: Site, binding: Any) -> asyncio.subprocess.Process:
    """A job that works for a long time, running; its process."""

    async def work() -> None:
        await run(s, binding, [])

    s.core.jobs._start("cmd_1", "opr_1", work)
    for _ in range(500):
        if "opr_1" in s.core.jobs._processes:
            return s.core.jobs._processes["opr_1"]
        await asyncio.sleep(0.01)
    raise AssertionError("the job did not start")


async def test_a_job_does_not_outlive_the_agent(tmp_path: Path, make_binding: Any) -> None:
    s = site(tmp_path, "import time; time.sleep(600)")
    process = await started(s, make_binding())
    runtime = AgentRuntime(s.settings, gateway=s.gateway)
    runtime.core = s.core
    try:
        await runtime.stop()
        assert process.returncode is not None  # stopped with the agent, and reaped
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
            await s.core.jobs.stop()
    (cancelled,) = [r for r in s.reports() if r.state == "Cancelled"]
    assert cancelled.message == "the agent stopped"  # not "cancelled from the control plane"


async def test_a_job_cancelled_any_way_takes_its_process_with_it(
    tmp_path: Path, make_binding: Any
) -> None:
    s = site(tmp_path, "import time; time.sleep(600)")
    process = await started(s, make_binding())
    task = s.core.jobs._tasks["opr_1"]
    task.cancel()  # not through SiteJobs.cancel: the event loop shutting down, say
    done, _ = await asyncio.wait({task}, timeout=10)
    try:
        assert done, "the cancelled job still waits for its process"
        assert process.returncode is not None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        with contextlib.suppress(asyncio.CancelledError):
            await task
