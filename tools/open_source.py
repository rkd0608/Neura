"""Assemble and check the open-source repository, neura-robot, from this one (design §30).

    python tools/open_source.py assemble DEST     # the published tree, written to DEST
    python tools/open_source.py check DEST        # what must hold before DEST is published

This repository stays the source: the open packages (``[tool.neura] open-source`` in
pyproject.toml) are developed here and mirrored one way. ``assemble`` copies their code and tests
(not their workspace manifests), the protocol sources and the example contracts, then the files
only the published repository has (``open-source/``: its README, licence, docs, CI, packaging and
standalone examples, which replace this repository's where both have one). Only tracked files are
copied, and the root conftest.py, whose fixtures (the example contracts) every package's
tests share.

``check`` refuses a tree that imports Neura's closed code outside a test skipped without it,
points at this repository's own files, or holds an address, key or token, and then installs it
fresh, alone, on the oldest and newest Python it supports and runs its lint, types, tests and
quickstart there. Docker tests and the LeRobot and MuJoCo extras are left to its CI.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "open-source"
PYTHONS = ("3.10", "3.13")

# Neura's closed packages: published code may not import them, published tests only behind
# pytest.importorskip (tools/check_boundary.py holds the same rule for this repository).
CLOSED = re.compile(
    r"^\s*(from|import) (neura_(server|sim|agent|control|adapters|bench|learn|sdk|cli))\b"
)
# Things that must not be published: this repository's own paths and tools, and secrets.
LEAKS = [
    (
        re.compile(r"docs/(neura-design|features|codemap)\.md|tools/[a-z_]+\.py|make verify"),
        "a path of Neura's repository",
    ),
    (re.compile(r"uv sync --group|--group models"), "Neura's workspace setup"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"(?i)\b(ghp|gho|github_pat|sk|xox[abp])[-_][A-Za-z0-9_]{16,}"), "a token"),
    (re.compile(r"[\w.+-]+@(gmail|googlemail|outlook|hotmail)\.com"), "a personal email address"),
]


def open_packages() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages: list[str] = config["tool"]["neura"]["open-source"]
    return packages


def tracked(*paths: str) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", *paths], cwd=ROOT, capture_output=True, check=True
    ).stdout
    return [Path(p) for p in out.decode().split("\0") if p]


def untracked() -> list[str]:
    """Files in the open packages or the template that git does not track: they would be left
    out of the published tree, which then differs from what was tested here."""
    paths = [f"packages/{p}" for p in open_packages()] + ["open-source"]
    out = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()


def assemble(dest: Path) -> list[Path]:
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty")
    missing = untracked()
    if missing:
        raise SystemExit(
            "not tracked by git, so not published (git add them): " + " ".join(missing)
        )
    dest.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for package in open_packages():
        for path in tracked(f"packages/{package}"):
            if path.name in ("pyproject.toml", "README.md") and len(path.parts) == 3:
                continue  # the workspace member's manifest: the published one is at the root
            files.append(path)
    files += tracked("proto/neura/v1", "examples/contracts", "conftest.py")
    for path in files:
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, target)
    published = [p.relative_to("open-source") for p in tracked("open-source")]
    for path in published:
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TEMPLATE / path, target)
    return sorted(set(files) | set(published))


def leaks(dest: Path) -> list[str]:
    found = []
    for path in sorted(p for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary: example data
        rel = path.relative_to(dest)
        skipped = set(re.findall(r'importorskip\(\s*"(neura_\w+)', text))
        for number, line in enumerate(text.splitlines(), 1):
            closed = CLOSED.match(line)
            if closed and not (rel.parts[-2] == "tests" and closed.group(2) in skipped):
                found.append(f"{rel}:{number}: imports Neura's closed {closed.group(2)}")
            for pattern, what in LEAKS:
                if pattern.search(line):
                    found.append(f"{rel}:{number}: {what}: {line.strip()[:100]}")
    return found


def run(command: list[str], cwd: Path, log: list[str]) -> bool:
    log.append(f"$ {' '.join(command)}")
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        log.append((done.stdout + done.stderr)[-4000:])
    return done.returncode == 0


def check(dest: Path) -> int:
    problems = leaks(dest)
    for line in problems:
        print(line, file=sys.stderr)
    if problems:
        print(f"{len(problems)} problems: nothing was installed or run", file=sys.stderr)
        return 1
    print("no closed imports, paths of this repository or secrets")
    failed = False
    for python in PYTHONS:
        with tempfile.TemporaryDirectory(prefix=f"neura-robot-{python}-") as scratch:
            venv = Path(scratch) / "venv"
            bin_ = venv / "bin"
            log: list[str] = []
            ok = run(["uv", "venv", "-q", "--python", python, str(venv)], dest, log) and run(
                [
                    "uv",
                    "pip",
                    "install",
                    "-q",
                    "--python",
                    str(bin_ / "python"),
                    "-e",
                    ".[dev,openpi]",
                ],
                dest,
                log,
            )
            steps = [
                ["ruff", "check", "."],
                ["ruff", "format", "--check", "."],
                ["mypy", "packages", "examples", "scripts"],
                ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                ["python", "examples/quickstart.py"],
            ]
            for step in steps:
                if not ok:
                    break
                ok = run([str(bin_ / step[0]), *step[1:]], dest, log)
            print(f"Python {python}: {'passed' if ok else 'FAILED'}")
            if not ok:
                failed = True
                print("\n".join(log), file=sys.stderr)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("assemble", "check"):
        sub.add_parser(name).add_argument("dest", type=Path)
    args = parser.parse_args()
    if args.command == "assemble":
        files = assemble(args.dest.resolve())
        print(f"{len(files)} files in {args.dest}")
        return 0
    return check(args.dest.resolve())


if __name__ == "__main__":
    sys.exit(main())
