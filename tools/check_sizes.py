"""Keep source files small enough to read whole.

    python tools/check_sizes.py            # fail if a file is over its budget
    python tools/check_sizes.py --update   # record the sizes of the files already over it

A file over BUDGET lines costs whoever reads it, a person or an agent, more than it gives: they
read far more than the part they need, or guess at the rest. A new file may not start over the
budget. Files already over it are recorded in tools/size-baseline.json with their size; each may
grow by at most SLACK lines, enough for a fix, not for a feature. Code added to one of them should
go to a new module instead, or come with code moved out. When one shrinks, --update records the
smaller size, so it cannot grow back; the check fails until it is recorded. Generated files are
exempt (tools/codemap.py lists them).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

from codemap import ROOT, SIZE_BASELINE, SOURCE, generated_by, tracked

BUDGET = 800
SLACK = 50


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    lines: int
    message: str


def count(path: str) -> int:
    with (ROOT / path).open("rb") as f:
        return sum(1 for _ in f)


def measured(path: str) -> bool:
    return path.endswith(SOURCE) and generated_by(path) is None


def baseline() -> dict[str, int]:
    if not SIZE_BASELINE.exists():
        return {}
    files: dict[str, int] = json.loads(SIZE_BASELINE.read_text(encoding="utf-8"))["files"]
    return files


def judge(path: str, lines: int, recorded: int | None) -> Finding | None:
    """What is wrong with this file's size, if anything."""
    if recorded is None:
        if lines > BUDGET:
            return Finding(
                path,
                lines,
                f"{path} is {lines} lines, over the {BUDGET}-line budget: split it by "
                "responsibility (each new module says what it is for in its first lines)",
            )
        return None
    if lines > recorded + SLACK:
        return Finding(
            path,
            lines,
            f"{path} is {lines} lines, {lines - recorded} more than its recorded {recorded}: move "
            f"code out of it (a new module) rather than adding to it",
        )
    if lines <= BUDGET:
        return Finding(
            path,
            lines,
            f"{path} is {lines} lines, under the budget now: run "
            "`python tools/check_sizes.py --update` to drop it from the baseline",
        )
    if lines < recorded - SLACK:
        return Finding(
            path,
            lines,
            f"{path} shrank from {recorded} to {lines} lines: run "
            "`python tools/check_sizes.py --update` so it cannot grow back",
        )
    return None


def check() -> list[Finding]:
    recorded = baseline()
    files = [p for p in tracked() if measured(p)]
    findings = [f for p in files if (f := judge(p, count(p), recorded.get(p)))]
    present = set(files)
    findings += [
        Finding(p, 0, f"{p} is in tools/size-baseline.json but no longer exists: run --update")
        for p in recorded
        if p not in present
    ]
    return findings


def update() -> None:
    sizes = {p: n for p in tracked() if measured(p) and (n := count(p)) > BUDGET}
    data = {
        "budget": BUDGET,
        "about": "Files over the budget when it was set, with their size. Shrink them; never grow "
        "them (tools/check_sizes.py).",
        "files": dict(sorted(sizes.items())),
    }
    SIZE_BASELINE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--update", action="store_true", help="record the current sizes")
    args = parser.parse_args()
    if args.update:
        update()
        print(f"recorded {len(baseline())} files over {BUDGET} lines in tools/size-baseline.json")
        return 0
    findings = check()
    for finding in findings:
        print(finding.message, file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
