"""The code map (docs/codemap.md), generated from what each file says it is for, and checks that
the maps and guides an agent reads stay true.

    python tools/codemap.py           # write docs/codemap.md
    python tools/codemap.py --check   # exit 1 if it is stale, or a guide names what is not there

Every source file outside the tests says what it is for in its first lines: a module docstring in
Python, a leading comment in TypeScript, CSS and shell. The map lists each file with the first
sentence of that, grouped by package, so a reader finds where something lives without opening
files. A source file that does not say what it is for fails the check.

The hand-written guides (CLAUDE.md, docs/features.md, the skills in .claude/skills) are checked
too: every path they name in backticks must exist, every ``file.py::test`` must name a test in that
file, and every ``make target`` must be a Makefile target. A guide that points at something moved
or deleted fails the check rather than misleading the next reader.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import functools
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "codemap.md"
SIZE_BASELINE = ROOT / "tools" / "size-baseline.json"
GUIDES = ("CLAUDE.md", "docs/features.md", ".claude/skills/*/SKILL.md")
SOURCE = (".py", ".ts", ".tsx", ".js", ".mjs", ".css", ".sh")

# Generated files: listed once each with what generates them, never described one by one.
GENERATED = {
    "packages/neura-proto/src/neura/v1/*": "tools/gen_proto.py, from proto/",
    "packages/neura-sdk/src/neura_sdk/_operations.py": "tools/gen_sdk.py, from api/openapi.yaml",
    "packages/neura-sdk/src/neura_sdk/models.py": "tools/gen_sdk.py, from api/openapi.yaml",
    "packages/neura-contracts/src/neura_contracts/reason_codes.py": (
        "tools/gen_reason_codes.py, from catalog/reason_codes.yaml"
    ),
    "packages/neura-contracts/src/neura_contracts/outbound_schema.py": (
        "tools/gen_outbound.py, from catalog/outbound.yaml"
    ),
    "console/src/generated/*": "tools/gen_reason_codes.py, from catalog/reason_codes.yaml",
    "console/src/api/schema.d.ts": "openapi-typescript (pnpm run gen), from api/openapi.yaml",
}
# Directories listed as one line: their files are many and alike.
COLLAPSED = {
    "packages/neura-control/src/neura_control/db/migrations/versions": "the database migrations",
}
# Top-level directories that are not packages or apps, and what each holds. A new top-level
# directory must be added here, so the map never silently leaves one out.
TOP_LEVEL = {
    ".claude": "The agent harness: settings (an edit hook) and skills (verify, debug, launch).",
    ".github": "CI workflows, Dependabot, the README's images and the PR template.",
    "api": "The public management API, spec first (openapi.yaml): the console, SDK and CLI use it.",
    "bench": "GPU-sharing benchmark reports (written by neura-bench).",
    "catalog": "Reason codes and the outbound-data allowlist: the sources of generated code.",
    "console": "The web console (React, TypeScript), its unit tests and its end-to-end tests.",
    "deploy": "Dockerfile, the Compose dev stack, nginx, installers, observability, cloud deploys.",
    "docs": "The engineering design (neura-design.md), this map and the feature map.",
    "examples": "Example contracts, robots and a site, used by tests and the docs.",
    "landing": "The NucleiLabs site (React, Vite).",
    "open-source": "The published neura-robot repository's own files (its code: packages/).",
    "packages": "The Python workspace: one package per component (below).",
    "proto": "The protobuf schema (neura.v1): the source of the generated bindings.",
    "qualification": "Hardware qualification reports (written by neura-qualify).",
    "schemas": "JSON Schemas for the contract documents (generated).",
    "security": "Accepted vulnerability exceptions, with reasons and expiry dates.",
    "site": "The developer docs (Docusaurus), served at /docs/ on the NucleiLabs site.",
    "soak": "Soak test reports (written by neura-sim-soak).",
    "tests": "Tests that span packages: the API spec, integration and the tools.",
    "tools": "Generators, checks, smoke tests and the agent harness's tools.",
}
APPS = ("console", "landing", "site")
SENTENCE = re.compile(r"(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)\.(?=\s|$)")
LONGEST = 200
BACKTICK = re.compile(r"`([^`\n]+)`")
MAKE = re.compile(r"\bmake ([a-z][a-z0-9-]*)")
PATHLIKE = re.compile(r"^[\w.@-]+(/[\w.@*-]+)+/?$|^[\w.-]+\.(md|py|ts|tsx|json|yaml|toml|mjs|sh)$")


@dataclass(slots=True)
class Entry:
    path: str  # relative to its group's root
    text: str


@dataclass(slots=True)
class Group:
    title: str
    description: str
    root: str
    tests_root: str
    entries: list[Entry] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    generated: list[tuple[str, str]] = field(default_factory=list)
    collapsed: list[tuple[str, str]] = field(default_factory=list)


def tracked() -> list[str]:
    """Every file git tracks or would add (untracked and not ignored)."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(p for p in out.splitlines() if (ROOT / p).is_file())


def generated_by(path: str) -> str | None:
    for pattern, source in GENERATED.items():
        if fnmatch.fnmatch(path, pattern):
            return source
    return None


def is_test(path: str) -> bool:
    name = Path(path).name
    return (
        "/tests/" in f"/{path}"
        or name.startswith("test_")
        or name == "conftest.py"
        or ".test." in name
        or (path.startswith("console/e2e/") and name.endswith(".spec.ts"))
    )


def first_sentence(text: str) -> str:
    paragraph = " ".join(text.strip().split("\n\n", 1)[0].split())
    match = SENTENCE.search(paragraph)
    sentence = paragraph[: match.start() + 1] if match else paragraph
    if len(sentence) > LONGEST:
        sentence = sentence[: sentence.rfind(" ", 0, LONGEST)] + " …"
    return sentence


def header_comment(text: str, suffix: str) -> str:
    """The comment a TypeScript, CSS or shell file starts with, without its markers."""
    lines = text.splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    out: list[str] = []
    if suffix == ".sh":
        for line in lines:
            if not line.startswith("#"):
                break
            out.append(line.lstrip("#").strip())
        return "\n".join(out)
    stripped = [line.strip() for line in lines]
    if stripped and stripped[0].startswith("/*"):
        for line in stripped:
            done = "*/" in line
            out.append(line.split("*/")[0].lstrip("/*").lstrip("*").strip())
            if done:
                break
        return "\n".join(out)
    for line in stripped:
        if line.startswith("///"):
            continue  # a TypeScript directive, not a comment
        if not line.startswith("//"):
            break
        out.append(line.removeprefix("//").strip())
    return "\n".join(out)


def describe(path: Path) -> str | None:
    """What a source file says it is for, or None when it says nothing."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        try:
            doc = ast.get_docstring(ast.parse(text))
        except SyntaxError:
            return None
    else:
        doc = header_comment(text, path.suffix)
    return first_sentence(doc) if doc and doc.strip() else None


def large_files() -> dict[str, int]:
    if not SIZE_BASELINE.exists():
        return {}
    data: dict[str, int] = json.loads(SIZE_BASELINE.read_text(encoding="utf-8"))["files"]
    return data


def groups(files: list[str]) -> tuple[list[Group], list[str]]:
    """The map's groups, and the source files that do not say what they are for."""
    out: list[Group] = []
    missing: list[str] = []

    def fill(group: Group, prefixes: tuple[str, ...]) -> None:
        for path in files:
            if not path.endswith(SOURCE) or not path.startswith(prefixes):
                continue
            rel = path.removeprefix(group.root + "/")
            source = generated_by(path)
            collapsed = next((c for c in COLLAPSED if path.startswith(c + "/")), None)
            if source is not None:
                # A generated directory is one entry; a generated file is its own.
                pattern = next(g for g in GENERATED if fnmatch.fnmatch(path, g))
                shown = pattern.removesuffix("*").removeprefix(group.root + "/")
                if (shown, source) not in group.generated:
                    group.generated.append((shown, source))
            elif collapsed is not None:
                entry = (collapsed.removeprefix(group.root + "/") + "/", COLLAPSED[collapsed])
                if entry not in group.collapsed:
                    group.collapsed.append(entry)
            elif is_test(path):
                group.tests.append(path.removeprefix(group.tests_root))
            elif not (ROOT / path).read_text(encoding="utf-8").strip():
                continue  # an empty __init__.py says nothing and needs nothing
            elif (text := describe(ROOT / path)) is None:
                missing.append(path)
            else:
                group.entries.append(Entry(rel, text))

    for project in sorted((ROOT / "packages").glob("*/pyproject.toml")):
        meta = tomllib.loads(project.read_text(encoding="utf-8"))["project"]
        root = str(project.parent.relative_to(ROOT))
        group = Group(root, meta.get("description", ""), f"{root}/src", f"{root}/tests/")
        fill(group, (f"{root}/src/", f"{root}/tests/"))
        out.append(group)
    for app in APPS:
        meta = json.loads((ROOT / app / "package.json").read_text(encoding="utf-8"))
        group = Group(app, meta.get("description", ""), app, f"{app}/")
        fill(group, (f"{app}/",))
        out.append(group)
    for name in ("deploy", "tools", "tests"):
        group = Group(name, TOP_LEVEL[name], name, f"{name}/")
        fill(group, (f"{name}/",))
        out.append(group)
    return out, missing


def render(files: list[str]) -> tuple[str, list[str]]:
    found, missing = groups(files)
    tops = sorted({p.split("/", 1)[0] for p in files if "/" in p})
    missing += [
        f"{t}/ (a top-level directory TOP_LEVEL does not describe)"
        for t in tops
        if t not in TOP_LEVEL
    ]
    lines = [
        "# Code map",
        "",
        "<!-- GENERATED FILE: do not edit. Source: each file's docstring or header comment.",
        "     Regenerate with `make gen`; `make check-generated` fails when it is stale. -->",
        "",
        "Where everything lives, from what each file says it is for. Read this before",
        "searching: it names the file, and the file's own first lines say more. What a feature",
        "touches end to end (code, tests, docs, how to prove it works) is in",
        "[features.md](features.md); why it is built that way is in",
        "[neura-design.md](neura-design.md).",
        "",
        "## The repository",
        "",
        "| Directory | What it holds |",
        "| --- | --- |",
    ]
    lines += [f"| `{t}/` | {TOP_LEVEL[t]} |" for t in TOP_LEVEL if (ROOT / t).is_dir()]
    big = large_files()
    if big:
        lines += [
            "",
            "## Large files",
            "",
            "Over the 800-line budget (tools/check_sizes.py). Search them and read the part you",
            "need; when you change one, move code out of it rather than adding to it.",
            "",
        ]
        lines += [f"- `{path}` ({count} lines)" for path, count in sorted(big.items())]
    for group in found:
        if not (group.entries or group.tests or group.generated):
            continue
        lines += ["", f"## {group.title}", ""]
        if group.description:
            lines += [group.description, ""]
        listed = [f"- `{e.path}`: {e.text}" for e in group.entries]
        listed += [f"- `{path}`: {text}" for path, text in group.collapsed]
        listed += [f"- `{path}`: generated by {source}" for path, source in group.generated]
        if listed:
            lines += [f"Under `{group.root}/`:", "", *listed, ""]
        if group.tests:
            names = ", ".join(f"`{t}`" for t in group.tests)
            lines += [f"Tests, under `{group.tests_root}`: {names}", ""]
        lines.pop()
    return "\n".join(lines) + "\n", missing


def guide_problems(files: list[str]) -> list[str]:
    """What the hand-written guides name that is not there."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-zA-Z][a-zA-Z0-9_-]*):", makefile, re.MULTILINE))
    known = set(files)
    directories = {"/".join(p.split("/")[:i]) for p in files for i in range(1, p.count("/") + 1)}
    guides = sorted({p for pattern in GUIDES for p in files if fnmatch.fnmatch(p, pattern)})
    problems: list[str] = []
    for guide in guides:
        text = (ROOT / guide).read_text(encoding="utf-8")
        problems += problems_in(guide, text, targets, known, directories)
    return problems


def problems_in(
    guide: str, text: str, targets: set[str], known: set[str], directories: set[str]
) -> list[str]:
    problems: list[str] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        # Commands are code: a code block's lines, or a code span in prose ("make it" is not).
        code = [line] if fenced else BACKTICK.findall(line)
        for target in (t for span in code for t in MAKE.findall(span)):
            if target not in targets:
                problems.append(f"{guide}:{number}: `make {target}` is not a Makefile target")
        if fenced:
            continue
        for token in BACKTICK.findall(line):
            problem = check_reference(token.strip(), known, directories)
            if problem:
                problems.append(f"{guide}:{number}: {problem}")
    return problems


@functools.cache
def ignored(path: str) -> bool:
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
    return result.returncode == 0


def check_reference(token: str, known: set[str], directories: set[str]) -> str | None:
    path, _, test = token.partition("::")
    if " " in path or path.startswith(("http", "-", "~", "$", "/")) or not PATHLIKE.match(path):
        return None
    if "<" in path or ignored(path):
        return None  # a placeholder, or what running something makes (git ignores it)
    if "*" in path:
        prefix = path.split("*", 1)[0]
        if not any(k.startswith(prefix) for k in known):
            return f"`{token}` matches nothing"
        return None
    if path.rstrip("/") in directories or path in known:
        if test and path in known:
            body = (ROOT / path).read_text(encoding="utf-8")
            name = test.split("[", 1)[0].split("::")[-1]
            if not re.search(rf"\bdef {re.escape(name)}\b", body):
                return f"`{token}`: no test {name} in {path}"
        return None
    if "/" not in path:
        return None  # a bare file name ("app.css") is only checked when it is a path
    return f"`{token}` does not exist"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="fail if stale, write nothing")
    args = parser.parse_args()
    files = tracked()
    text, missing = render(files)
    problems = [
        f"{path}: say what it is for in its first lines (a docstring, or a header comment)"
        for path in missing
    ]
    problems += guide_problems(files)
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            problems.append(f"{OUT.relative_to(ROOT)} is stale: run `make gen`")
    elif not missing:
        OUT.write_text(text, encoding="utf-8")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
