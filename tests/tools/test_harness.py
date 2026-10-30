"""The agent harness's tools: the code map and the guides' checks, the size budget, the edit hook,
verify's choice of checks, the dev report, and the screenshot tool catching a broken page."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import check_sizes  # noqa: E402
import codemap  # noqa: E402
import dev_report  # noqa: E402
import verify  # noqa: E402

# --- the code map ----------------------------------------------------------------------------


def test_a_description_is_the_first_sentence() -> None:
    assert codemap.first_sentence("Leases: who may be served.\n\nMore.") == (
        "Leases: who may be served."
    )
    assert codemap.first_sentence("Units, e.g. degrees. Then more.") == "Units, e.g. degrees."
    long = "word " * 80
    assert codemap.first_sentence(long).endswith(" …")
    assert len(codemap.first_sentence(long)) <= codemap.LONGEST + 2


@pytest.mark.parametrize(
    ("suffix", "text", "expected"),
    [
        (".ts", "// The fleet's words.\n// More.\nimport x;\n", "The fleet's words."),
        (".ts", '/// <reference types="vitest" />\n// The build.\n', "The build."),
        (".css", "/* The styles.\n   More. */\n:root {}\n", "The styles."),
        (".sh", "#!/bin/sh\n# Install the agent.\nset -eu\n", "Install the agent."),
        (".tsx", "import x from 'y';\n// Too late.\n", None),
    ],
)
def test_files_say_what_they_are_for_in_their_first_lines(
    tmp_path: Path, suffix: str, text: str, expected: str | None
) -> None:
    path = tmp_path / f"file{suffix}"
    path.write_text(text, encoding="utf-8")
    assert codemap.describe(path) == expected


def test_a_python_module_without_a_docstring_says_nothing(tmp_path: Path) -> None:
    path = tmp_path / "module.py"
    path.write_text("import os\n", encoding="utf-8")
    assert codemap.describe(path) is None


def _guide(text: str) -> list[str]:
    known = {"packages/a/src/a/b.py", "packages/a/tests/test_b.py"}
    directories = {
        "packages",
        "packages/a",
        "packages/a/src",
        "packages/a/src/a",
        "packages/a/tests",
    }
    return codemap.problems_in("guide.md", text, {"verify", "gen"}, known, directories)


def test_guides_may_name_only_what_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codemap, "ignored", lambda path: path.startswith(".verify"))
    assert _guide("See `packages/a/src/a/b.py` and `packages/a/src/`.") == []
    assert _guide("Run `make verify`; then make it pass.") == []  # prose is not a command
    assert _guide("```bash\nmake verify\nmake gen\n```") == []
    assert _guide("Logs in `.verify/pytest.log`.") == []  # made by running something
    assert _guide("`packages/a/src/a/gone.py`") == [
        "guide.md:1: `packages/a/src/a/gone.py` does not exist"
    ]
    assert _guide("`make nope`") == ["guide.md:1: `make nope` is not a Makefile target"]
    assert _guide("```\nmake nope\n```") == ["guide.md:2: `make nope` is not a Makefile target"]


def test_a_named_test_must_be_in_its_file() -> None:
    real = "tests/tools/test_harness.py"
    known = {real}
    directories = {"tests", "tests/tools"}
    name = "test_a_named_test_must_be_in_its_file"
    assert codemap.check_reference(f"{real}::{name}", known, directories) is None
    assert codemap.check_reference(f"{real}::test_nothing", known, directories) == (
        f"`{real}::test_nothing`: no test test_nothing in {real}"
    )


def test_the_map_lists_every_package_and_the_large_files() -> None:
    text = codemap.OUT.read_text(encoding="utf-8")
    for project in (ROOT / "packages").glob("*/pyproject.toml"):
        assert f"## packages/{project.parent.name}\n" in text
    for path in check_sizes.baseline():
        assert f"`{path}`" in text


# --- the size budget -------------------------------------------------------------------------


def test_the_size_budget() -> None:
    budget, slack = check_sizes.BUDGET, check_sizes.SLACK
    assert check_sizes.judge("new.py", budget, None) is None
    assert check_sizes.judge("new.py", budget + 1, None) is not None  # a new file over it
    assert check_sizes.judge("big.py", 1000 + slack, 1000) is None  # room for a fix
    assert check_sizes.judge("big.py", 1001 + slack, 1000) is not None  # not for a feature
    assert check_sizes.judge("big.py", 1000 - slack, 1000) is None
    shrank = check_sizes.judge("big.py", 999 - slack, 1000)
    assert shrank is not None
    assert "--update" in shrank.message  # record it, so it cannot grow back
    under = check_sizes.judge("big.py", budget, 1000)
    assert under is not None
    assert "--update" in under.message


def test_generated_files_have_no_budget() -> None:
    assert not check_sizes.measured("packages/neura-sdk/src/neura_sdk/models.py")
    assert check_sizes.measured("packages/neura-agent/src/neura_agent/core.py")
    assert not check_sizes.measured("README.md")


# --- the edit hook ---------------------------------------------------------------------------


def _hook(event: object) -> str:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "edit_hook.py")],
        input=event if isinstance(event, str) else json.dumps(event),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_the_edit_hook_tells_the_agent_what_an_edit_needs() -> None:
    proto = str(ROOT / "proto" / "neura" / "v1" / "site.proto")
    out = json.loads(_hook({"tool_name": "Edit", "tool_input": {"file_path": proto}}))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "make gen" in out["hookSpecificOutput"]["additionalContext"]


def test_the_edit_hook_never_stands_in_the_way() -> None:
    assert _hook("not json") == ""
    assert _hook({"tool_name": "Edit", "tool_input": {"file_path": "/etc/hostname"}}) == ""
    assert _hook({"tool_name": "Edit", "tool_input": {}}) == ""


def test_the_hook_is_configured_for_every_edit() -> None:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    (entry,) = settings["hooks"]["PostToolUse"]
    assert set(entry["matcher"].split("|")) == {"Edit", "Write", "MultiEdit"}
    assert "tools/edit_hook.py" in entry["hooks"][0]["command"]


# --- verify ----------------------------------------------------------------------------------


def test_a_package_change_runs_the_tests_of_everything_that_depends_on_it() -> None:
    graph = verify.packages()
    affected = verify.affected_packages({"neura-runtime"}, graph)
    assert {"neura-runtime", "neura-server", "neura-agent", "neura-sim"} <= affected
    assert "neura-control" not in affected  # it does not depend on the runtime
    tests, why = verify.select_tests(
        ["packages/neura-runtime/src/neura_runtime/action_queue.py"], everything=False
    )
    assert tests is not None
    assert "packages/neura-runtime/tests/test_action_queue.py" in tests
    assert "packages/neura-server/tests/test_gateway.py" in tests
    # The control plane's end-to-end tests run a real agent, so they run too; its API tests do not.
    assert "packages/neura-control/tests/test_site_agent_e2e.py" in tests
    assert "packages/neura-control/tests/test_members.py" not in tests
    assert "neura-runtime" in why


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["conftest.py"], None),
        (["examples/contracts/so101.robot.yaml"], None),
        (["tools/codemap.py"], ["tests/tools"]),
        (
            ["packages/neura-sim/tests/test_invariants.py"],
            ["packages/neura-sim/tests/test_invariants.py"],
        ),
        (["README.md"], []),
    ],
)
def test_what_verify_tests_for_a_change(changed: list[str], expected: list[str] | None) -> None:
    tests, _ = verify.select_tests(changed, everything=False)
    assert tests == expected


def _plan(files: list[str], **flags: bool) -> list[str]:
    args = argparse.Namespace(**({"all": False, "e2e": False, "models": False} | flags))
    checks, _ = verify.plan(args, files)
    return [c.name for c in checks]


def test_a_site_change_is_looked_at_and_a_python_change_is_typed() -> None:
    assert _plan(["landing/src/App.tsx"]) == ["sizes", "maps", "landing", "screenshots"]
    python = _plan(["packages/neura-sim/src/neura_sim/site.py"])
    assert {"ruff", "mypy", "generated", "pytest"} <= set(python)
    assert "screenshots" not in python
    assert "e2e" in _plan(["README.md"], e2e=True)


@pytest.fixture
def logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Verify's logs, away from the real .verify/: a running verify runs these tests."""
    monkeypatch.setattr(verify, "LOGS", tmp_path)
    return tmp_path


def test_verify_reads_the_test_summary(logs: Path) -> None:
    check = verify.Check("pytest", ["true"], "why")
    check.log.write_text("...\n3 failed, 40 passed, 2 skipped in 9.10s\n", encoding="utf-8")
    detail = verify.summary_of(check)
    assert check.log.parent == logs
    assert detail.startswith("3 failed, 40 passed, 2 skipped in 9.10s")
    assert "proves nothing" in detail


def test_a_check_that_cannot_run_fails(logs: Path) -> None:
    check = verify.Check("landing", ["no-such-tool-anywhere"], "why", cwd=ROOT / "landing")
    verify.run(check)
    assert check.status == "failed"
    assert "not installed" in check.detail


def test_a_check_that_leaves_temporary_files_fails(logs: Path) -> None:
    leaky = "import tempfile; tempfile.mkdtemp(prefix='leak-')"
    check = verify.Check("pytest", [sys.executable, "-c", leaky], "why", clean=True)
    verify.run(check)
    assert check.status == "failed"
    assert "left 1 in TMPDIR (leak-" in check.detail
    tidy = verify.Check("pytest", [sys.executable, "-c", "pass"], "why", clean=True)
    verify.run(tidy)
    assert tidy.status == "passed"


def test_the_tests_temporary_directory_is_short_and_removed(logs: Path) -> None:
    """Unix sockets made under TMPDIR must fit about 108 bytes, from any checkout or worktree."""
    show = "import os, tempfile; print('TMPDIR=' + tempfile.gettempdir())"
    check = verify.Check("pytest", [sys.executable, "-c", show], "why", clean=True)
    verify.run(check)
    (line,) = [
        x for x in check.log.read_text(encoding="utf-8").splitlines() if x.startswith("TMPDIR=")
    ]
    used = Path(line.split("=", 1)[1])
    assert not used.is_relative_to(ROOT)
    assert len(str(used)) + len("/neura-w-xxxxxxxx/g1-12.sock") < 100
    assert not used.exists()


def test_timing_checks_run_alone(logs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    running: set[str] = set()
    beside: dict[str, set[str]] = {}
    lock = threading.Lock()

    def run(check: verify.Check) -> None:
        with lock:
            beside[check.name] = set(running)
            running.add(check.name)
        time.sleep(0.05)
        with lock:
            running.discard(check.name)
        check.status = "passed"

    monkeypatch.setattr(verify, "run", run)
    checks = [
        verify.Check("mypy", ["true"], "why"),
        verify.Check("console", ["true"], "why"),
        verify.Check("pytest", ["true"], "why", alone=True),
        verify.Check("e2e", ["true"], "why", alone=True),
    ]
    verify.execute(checks)
    assert beside["pytest"] == set()
    assert beside["e2e"] == set()
    assert all(c.status == "passed" for c in checks)


def test_a_check_that_breaks_verify_is_reported_not_lost(
    logs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(check: verify.Check) -> str:
        raise FileNotFoundError(check.log)

    monkeypatch.setattr(verify, "summary_of", broken)
    checks = [verify.Check("pytest", ["true"], "why"), verify.Check("mypy", ["true"], "why")]
    verify.execute(checks)
    assert [c.status for c in checks] == ["failed", "failed"]
    assert "verify itself failed" in checks[0].detail
    assert "FileNotFoundError" in checks[0].log.read_text(encoding="utf-8")


# --- the dev report --------------------------------------------------------------------------

TRACEBACK = """\
INFO started
Traceback (most recent call last):
  File "core.py", line 3, in run
    reconcile()
ValueError: the revision is not committed
INFO still going
2026-10-25 ERROR neura_agent.connection: stream closed
"""


def test_the_report_puts_failures_first_with_their_stack() -> None:
    report = dev_report.Report()
    report.scan("site lab, agent.log", TRACEBACK)
    first, second = report.failures
    assert "ValueError: the revision is not committed" in first
    assert 'File "core.py", line 3' in first
    assert "stream closed" in second


@pytest.fixture
def status_server() -> Iterator[int]:
    class Status(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps({"enrolled": True, "path": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Status)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_the_report_finds_each_local_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_server: int
) -> None:
    site = tmp_path / "neura" / "sites" / "lab"
    site.mkdir(parents=True)
    (site / "agent.env").write_text(f"NEURA_AGENT_LOCAL_PORT={status_server}\n", encoding="utf-8")
    (site / "agent.pid").write_text(str(os.getpid()), encoding="utf-8")
    (site / "agent.log").write_text(TRACEBACK, encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    report = dev_report.Report()
    dev_report.local_sites(report, lines=50)
    (section,) = report.sections
    assert "## Site lab" in section
    assert "(running)" in section
    assert 'HTTP 200: {"enrolled":true,"path":"/v1/local/status"}' in section
    assert any("ValueError" in f for f in report.failures)


# --- the screenshot tool ---------------------------------------------------------------------


def test_the_screenshot_tool_fails_a_broken_page(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None or not (ROOT / "console" / "node_modules" / "@playwright").exists():
        pytest.skip("needs node and the console's Playwright (make console-setup)")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        "<!doctype html><title>t</title><body>"
        "<script>window.x = 1</script>"  # refused by the site's Content-Security-Policy
        '<div style="width:2000px">wide</div>'
        '<img src="/missing.png">'
        "</body>",
        encoding="utf-8",
    )
    out = tmp_path / "shots"
    result = subprocess.run(
        [
            node,
            str(ROOT / "tools" / "shoot.mjs"),
            "--only",
            "site",
            "--site",
            str(site),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    problems = "\n".join(p for shot in report for p in shot["problems"])
    assert "Content Security Policy" in problems
    assert "wider than the screen" in problems
    # Like production's nginx, the site answers an unknown path with its page: the image is broken.
    assert "image did not load" in problems
    assert (out / "home-phone-dark.png").exists()
