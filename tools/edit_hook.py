"""What the agent is told right after it writes a file (Claude Code's PostToolUse hook, configured
in .claude/settings.json).

Reads the hook's event on stdin and, when the file written needs something more, prints a note
the agent reads before it goes on:

- a source file over its size budget, or grown past its recorded size (tools/check_sizes.py);
- a source file that does not say what it is for in its first lines, or whose entry in
  docs/codemap.md no longer matches (tools/codemap.py);
- a source of generated files (proto/, catalog/, api/openapi.yaml, the contract models, example
  contracts), so the generated files are refreshed in the same change.

It never blocks an edit and never fails: a broken hook must not stop the work, and the same
checks run again, as failures, in `make verify`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GENERATED_FROM = {
    "proto/": "make gen",
    "catalog/": "make gen",
    "api/openapi.yaml": "make gen",
    "packages/neura-contracts/src/neura_contracts/models.py": "make gen",
    "examples/contracts/": "make examples",
    "tools/gen_": "make gen",
}


def notes(path: str) -> list[str]:
    from check_sizes import BUDGET, baseline, count, judge, measured
    from codemap import OUT, describe, is_test, render, tracked

    out: list[str] = []
    for prefix, command in GENERATED_FROM.items():
        if path.startswith(prefix):
            out.append(f"{path} is a source of generated files: run `{command}` in this change.")
    if not measured(path):
        return out
    lines = count(path)
    recorded = baseline().get(path)
    finding = judge(path, lines, recorded)
    if finding is not None and lines > BUDGET:
        out.append(finding.message + ".")
    elif recorded is not None and lines > recorded:
        out.append(
            f"{path} is one of the large files ({lines} lines, recorded {recorded}): move code "
            "out of it rather than adding to it."
        )
    if not is_test(path) and describe(OUT.parent.parent / path) is None:
        out.append(
            f"{path} does not say what it is for: start it with a docstring (Python) or a "
            "comment (TypeScript, CSS, shell) whose first sentence does; docs/codemap.md lists it."
        )
    elif render(tracked())[0] != OUT.read_text(encoding="utf-8"):
        out.append("docs/codemap.md no longer matches what the files say: run `make gen`.")
    return out


def main() -> int:
    try:
        event = json.load(sys.stdin)
        root = Path(__file__).resolve().parent.parent
        raw = event.get("tool_input", {}).get("file_path") or ""
        path = Path(raw).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            return 0
        sys.path.insert(0, str(root / "tools"))
        found = notes(str(path.relative_to(root)))
    except Exception:  # the hook must never stand in the way of an edit
        return 0
    if found:
        context = "\n".join(found)
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": context,
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
