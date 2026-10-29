"""Prove a change works before saying it does: run the checks the change can break, keep their
logs, and say plainly what passed, what failed and what was not checked.

    python tools/verify.py              # the checks for what changed since origin/main
    python tools/verify.py --all        # every check and the whole test suite
    python tools/verify.py --e2e        # also the console's end-to-end journey, in Chromium
    python tools/verify.py --models     # also the model tests (needs `make models-setup`)
    python tools/verify.py --plan       # say what would run, and why, without running it

What changed is everything since the merge base with origin/main: commits, staged and unstaged
edits, and new files. From it come the checks: lint and types when Python changed; the tests of
the packages changed, of every package that depends on them, and of every test file that imports
them; the console, the NucleiLabs site and the docs when they changed, with screenshots of the
site and the docs in a browser, which fail on any console error or failed request.

Each check's output is in .verify/<check>.log; the summary is .verify/summary.md. Tests that need
Postgres run when it answers on 127.0.0.1:5432 (the summary says which), and every skipped test
is counted: a skipped test proves nothing. The exit status is 1 when any check failed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import traceback
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / ".verify"
DATABASE = "postgresql://neura:neura@127.0.0.1:5432/neura"
TOKENS = "console/src/styles/tokens.css"  # shared by the console, the site and the docs
FRONTENDS = ("console/", "landing/", "site/")
# Files outside Python whose tests are not found by imports.
COVERED_BY = {
    "deploy/install/": ["tests/integration/test_install_upgrade.py"],
    "deploy/console/": ["tests/integration/test_console_proxy.py"],
    "deploy/": ["tests/tools/test_supply_chain.py"],
    ".github/dependabot.yml": ["tests/tools/test_supply_chain.py"],
    "deploy/observability/": ["tests/integration/test_observability.py"],
    "api/openapi.yaml": ["tests/api", "packages/neura-control/tests"],
    "catalog/": ["tests/tools", "packages/neura-contracts/tests"],
    "site/docs/": ["tests/tools/test_docs.py"],
    "tools/": ["tests/tools"],
}
# Changes no automatic check covers, and what does.
BY_HAND = {
    ".github/workflows/": "CI workflows run only on GitHub",
    "deploy/docker/": "the images: `make smoke` builds and runs them",
    "deploy/compose/": "the dev stack: `make smoke`",
    "deploy/gcp/": "the cloud deploy: run only by the deploy workflow",
    "deploy/modal/": "the Modal benchmark runner: run only by its workflow",
}
SETUP = {"console": "console-setup", "landing": "landing-setup", "site": "docs-setup"}


@dataclass(slots=True)
class Check:
    name: str
    command: list[str]
    why: str
    cwd: Path = ROOT
    env: dict[str, str] = field(default_factory=dict)
    after: tuple[str, ...] = ()  # checks that must finish first (their builds)
    clean: bool = False  # run with an empty TMPDIR, and fail on anything left in it
    status: str = "not run"
    seconds: float = 0.0
    detail: str = ""

    @property
    def log(self) -> Path:
        return LOGS / f"{self.name}.log"


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return [line for line in out.stdout.splitlines() if line]


def changed_files(base: str) -> tuple[str, list[str]]:
    """The merge base used, and every file changed since it (committed or not)."""
    for ref in (base, "origin/main", "main"):
        found = git("merge-base", ref, "HEAD")
        if found:
            merge_base = found[0]
            break
    else:
        merge_base = "HEAD"
    files = set(git("diff", "--name-only", merge_base))
    files |= set(git("ls-files", "--others", "--exclude-standard"))
    return merge_base[:12], sorted(files)


def packages() -> dict[str, tuple[set[str], set[str]]]:
    """Each workspace package: the modules it provides, and the packages it depends on."""
    out: dict[str, tuple[set[str], set[str]]] = {}
    for project in sorted((ROOT / "packages").glob("*/pyproject.toml")):
        meta = tomllib.loads(project.read_text(encoding="utf-8"))["project"]
        deps = {re.split(r"[<>=\[ ;~!]", d, maxsplit=1)[0] for d in meta.get("dependencies", [])}
        src = project.parent / "src"
        modules = {p.name for p in src.iterdir() if p.is_dir() and not p.name.startswith("_")}
        out[project.parent.name] = (modules, {d for d in deps if d.startswith("neura")})
    return out


def affected_packages(changed: set[str], graph: dict[str, tuple[set[str], set[str]]]) -> set[str]:
    """The packages changed and every package that depends on one of them, however indirectly."""
    found = set(changed)
    while True:
        more = {name for name, (_, deps) in graph.items() if deps & found} - found
        if not more:
            return found
        found |= more


def imported(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return {m.split(".")[0] for m in re.findall(r"^\s*(?:from|import)\s+([\w.]+)", text, re.M)}


def test_files() -> list[str]:
    return [
        p
        for p in git("ls-files", "--cached", "--others", "--exclude-standard", "*.py")
        if ("/tests/" in f"/{p}") and Path(p).name.startswith("test_") and (ROOT / p).is_file()
    ]


def select_tests(files: list[str], everything: bool) -> tuple[list[str] | None, str]:
    """The test files and directories to run (None: the whole suite), and why."""
    if everything:
        return None, "--all: the whole suite"
    graph = packages()
    module_of = {m: name for name, (modules, _) in graph.items() for m in modules}
    touched: set[str] = set()
    targets: set[str] = set()
    reasons: list[str] = []
    for path in files:
        parts = path.split("/")
        if path.endswith(".py") and Path(path).name.startswith("test_") and (ROOT / path).exists():
            targets.add(path)
        elif Path(path).name == "conftest.py":
            if path == "conftest.py":
                return None, "conftest.py (the shared fixtures) changed: the whole suite"
            targets.add(str(Path(path).parent))
        elif parts[0] == "packages" and len(parts) > 3 and parts[2] == "src":
            touched.add(parts[1])
        elif parts[0] == "proto":
            touched.add("neura-proto")
        elif parts[0] == "examples":
            return None, "examples/ (the shared fixtures) changed: the whole suite"
        for prefix, tests in COVERED_BY.items():
            if path.startswith(prefix):
                targets.update(t for t in tests if (ROOT / t).exists())
    if touched:
        names = affected_packages(touched, graph)
        reasons.append(f"{', '.join(sorted(names))} (changed, or depending on what did)")
        modules = {m for m, name in module_of.items() if name in names}
        for test in test_files():
            if imported(ROOT / test) & modules:
                targets.add(test)
    # A directory already selected covers the files in it.
    dirs = {t for t in targets if (ROOT / t).is_dir()}
    kept = sorted(t for t in targets if not any(t.startswith(d + "/") for d in dirs))
    if kept and not reasons:
        reasons.append("test files changed, or what they cover")
    return kept, "; ".join(reasons)


def uncovered(files: list[str]) -> list[str]:
    """Changed files that no check here verifies, with what does."""
    notes: list[str] = []
    for path in files:
        hand = next((why for prefix, why in BY_HAND.items() if path.startswith(prefix)), None)
        if hand and hand not in notes:
            notes.append(hand)
    return notes


def postgres() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 5432), timeout=1):
            return True
    except OSError:
        return False


def plan(args: argparse.Namespace, files: list[str]) -> tuple[list[Check], list[str]]:
    python = [sys.executable]

    def has(*prefixes: str) -> bool:
        return bool(args.all or any(f.startswith(prefixes) for f in files))

    py = args.all or any(f.endswith(".py") or f == "pyproject.toml" for f in files)
    non_front = args.all or any(not f.startswith(FRONTENDS) for f in files)
    checks = [
        Check("sizes", [*python, "tools/check_sizes.py"], "always: files stay readable whole"),
        Check("maps", [*python, "tools/codemap.py", "--check"], "always: the maps stay true"),
    ]
    if py:
        checks += [
            Check(
                "ruff",
                [
                    "sh",
                    "-c",
                    f"{python[0]} -m ruff check . && {python[0]} -m ruff format --check .",
                ],
                "Python changed",
            ),
            Check(
                "mypy",
                [*python, "-m", "mypy", "packages", "tools", "tests", "console/e2e"],
                "Python changed",
            ),
        ]
    if non_front:
        checks.append(
            Check(
                "generated",
                ["make", "--no-print-directory", "check-generated"],
                "generated files must match their sources",
            )
        )
    tests, why = select_tests(files, args.all)
    env = {"NEURA_TEST_DATABASE_URL": os.environ.get("NEURA_TEST_DATABASE_URL", DATABASE)}
    database = "NEURA_TEST_DATABASE_URL" in os.environ or postgres()
    if database:
        why += "; with Postgres"
    else:
        env = {}
    if tests is None or tests:
        checks.append(
            Check(
                "pytest",
                [*python, "-m", "pytest", "-q", "-ra", f"--basetemp={LOGS / 'pytest-tmp'}"]
                + (tests or []),
                why,
                env=env,
                clean=True,
            )
        )
    if has("console/", TOKENS, "api/openapi.yaml"):
        checks.append(
            Check(
                "console",
                ["sh", "-c", "pnpm run check && pnpm run check:gen"],
                "the console changed",
                cwd=ROOT / "console",
            )
        )
    if has("landing/", TOKENS):
        checks.append(
            Check(
                "landing",
                ["pnpm", "run", "check"],
                "the NucleiLabs site changed",
                cwd=ROOT / "landing",
            )
        )
    if has("site/", TOKENS):
        checks.append(
            Check("docs", ["make", "--no-print-directory", "docs-check"], "the docs changed")
        )
    shots = [c.name for c in checks if c.name in ("landing", "docs")]
    if shots:
        only = ",".join("docs" if n == "docs" else "site" for n in shots)
        checks.append(
            Check(
                "screenshots",
                ["node", "tools/shoot.mjs", "--only", only, "--out", ".verify/shots"],
                "the site or docs changed: look at them in a browser",
                after=tuple(shots),
            )
        )
    if args.e2e:
        e2e_env = {
            "NEURA_E2E_DATABASE_URL": DATABASE,
            "NEURA_E2E_SCREENSHOTS": str(LOGS / "console"),
        }
        if Path("/opt/pw-browsers/chromium").exists() and "NEURA_CHROMIUM" not in os.environ:
            e2e_env["NEURA_CHROMIUM"] = "/opt/pw-browsers/chromium"
        checks.append(
            Check(
                "e2e",
                ["pnpm", "run", "e2e"],
                "--e2e: the console's journey",
                cwd=ROOT / "console",
                env=e2e_env,
            )
        )
    if args.models:
        checks.append(
            Check(
                "models", ["make", "--no-print-directory", "test-models"], "--models: real models"
            )
        )
    notes = uncovered(files)
    if not database and any(c.name == "pytest" for c in checks):
        notes.append(
            "Postgres is not answering on 127.0.0.1:5432, so the tests that need it "
            "skip (`service postgresql start`, or `make dev`)"
        )
    if not args.e2e:
        notes.append("the console's end-to-end journey (--e2e)")
    if not args.models:
        notes.append("the model tests (--models)")
    return checks, notes


def run(check: Check) -> None:
    started = time.monotonic()
    app = check.cwd.name if check.cwd != ROOT else ("site" if check.name == "docs" else "")
    if not shutil.which(check.command[0]):
        problem = f"{check.command[0]} is not installed"
    elif app in SETUP and not (ROOT / app / "node_modules").exists():
        problem = f"{app}'s dependencies are not installed: `make {SETUP[app]}`"
    else:
        problem = ""
    if problem:
        # Not being able to run a check is not a pass.
        check.status, check.detail = "failed", problem
        check.log.write_text(problem + "\n", encoding="utf-8")
        return
    env = {**os.environ, **check.env}
    scratch = LOGS / f"tmp-{check.name}"
    if check.clean:
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        env["TMPDIR"] = str(scratch)
        # PyTorch keeps its compiled kernels under TMPDIR by default: a cache, kept between runs.
        env.setdefault("TORCHINDUCTOR_CACHE_DIR", str(LOGS / "torch-cache"))
    with check.log.open("w", encoding="utf-8") as log:
        log.write(f"$ (cd {check.cwd.relative_to(ROOT) or '.'}) {' '.join(check.command)}\n\n")
        log.flush()
        result = subprocess.run(
            check.command,
            cwd=check.cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=3600,
        )
    check.seconds = time.monotonic() - started
    check.status = "passed" if result.returncode == 0 else "failed"
    check.detail = summary_of(check)
    left = sorted(p.name for p in scratch.iterdir()) if check.clean else []
    if left:
        # Something did not clean up after itself: every run would leave this behind.
        check.status = "failed"
        check.detail = f"left {len(left)} in TMPDIR ({', '.join(left[:3])}); {check.detail}"
        with check.log.open("a", encoding="utf-8") as log:
            log.write(f"\nLeft in {scratch}, which was empty before:\n" + "\n".join(left) + "\n")


def summary_of(check: Check) -> str:
    text = check.log.read_text(encoding="utf-8", errors="replace")
    if check.name in ("pytest", "models"):
        lines: list[str] = re.findall(
            r"^=*\s*(\d+ (?:passed|failed|errors?|skipped)\b.*? in [\d.]+s)", text, re.M
        )
        if lines:
            skipped = re.search(r"(\d+) skipped", lines[-1])
            note = " (a skipped test proves nothing: see the log)" if skipped else ""
            return lines[-1] + note
    if check.name == "screenshots":
        found = re.findall(r"^shots: .*$", text, re.M)
        return found[-1] if found else ""
    return ""


def tail(path: Path, count: int = 40) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-count:])


def guarded(check: Check) -> None:
    """Run a check; if verify itself breaks on it, that check failed, and the others still count."""
    try:
        run(check)
    except Exception as error:
        check.status, check.detail = "failed", f"verify itself failed: {error!r}"
        LOGS.mkdir(exist_ok=True)
        with check.log.open("a", encoding="utf-8") as log:
            log.write("\n" + traceback.format_exc())


def execute(checks: list[Check]) -> None:
    LOGS.mkdir(exist_ok=True)
    by_name = {c.name: c for c in checks}
    first = [c for c in checks if c.name in ("sizes", "maps", "ruff")]
    for check in first:
        guarded(check)
    rest = [c for c in checks if c not in first and not c.after]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(guarded, rest))
    for check in (c for c in checks if c.after):
        blocked = [n for n in check.after if by_name[n].status != "passed"]
        if blocked:
            check.status, check.detail = "not run", f"needs {', '.join(blocked)} to pass first"
        else:
            guarded(check)


def report(base: str, files: list[str], checks: list[Check], notes: list[str]) -> str:
    mark = {"passed": "✓", "failed": "✗", "not run": "-"}
    lines = [f"verify: {len(files)} files changed since {base}", ""]
    for c in checks:
        timing = f"{c.seconds:5.1f}s" if c.seconds else "      "
        lines.append(f"  {mark[c.status]} {c.name:<12} {timing}  {c.detail or c.why}")
    if notes:
        lines += ["", "Not verified here:"] + [f"  - {n}" for n in notes]
    failed = [c for c in checks if c.status == "failed"]
    for c in failed:
        lines += ["", f"--- {c.name} failed; the end of {c.log.relative_to(ROOT)}:", tail(c.log)]
    lines += [
        "",
        "FAILED: " + ", ".join(c.name for c in failed) if failed else "All checks passed.",
    ]
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--all", action="store_true", help="every check, the whole suite")
    parser.add_argument("--e2e", action="store_true", help="also the console's end-to-end tests")
    parser.add_argument("--models", action="store_true", help="also the model tests")
    parser.add_argument("--plan", action="store_true", help="say what would run; run nothing")
    parser.add_argument("--base", default="origin/main", help="compare with (default origin/main)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    base, files = changed_files(args.base)
    if not files and not args.all:
        print(f"verify: nothing changed since {base}; --all checks everything")
        return 0
    checks, notes = plan(args, files)
    if args.plan:
        print(f"verify: {len(files)} files changed since {base}; would run:")
        for c in checks:
            print(f"  {c.name:<12} {c.why}\n  {'':<12} $ {' '.join(c.command)}")
        print("\nNot verified here:\n" + "\n".join(f"  - {n}" for n in notes))
        return 0
    LOGS.mkdir(exist_ok=True)
    execute(checks)
    text = report(base, files, checks, notes)
    (LOGS / "summary.md").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if any(c.status == "failed" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
