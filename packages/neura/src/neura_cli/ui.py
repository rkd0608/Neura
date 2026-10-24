"""How the CLI talks to people (design §29, "Rules that keep it clean").

One look everywhere: a header saying what is being done, one line per step with its mark and its
reason, a link to the same thing in the console, and nothing else unless asked. Colour only marks
state (and only on a terminal, never with ``NO_COLOR``); ``--json`` prints the API's objects for
scripts instead.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

MARKS = {
    "passed": "✓",
    "skipped": "–",  # noqa: RUF001 - a dash, on purpose
    "failed": "✗",
    "running": "◐",
    "pending": " ",
}
STEP_LABELS = {
    "check": "Fits the robots",
    "prepare": "At the site",
    "shadow": "Shadow test",
    "approve": "Approval",
    "switch": "Switch",
}


@dataclass
class Style:
    color: bool

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def good(self, text: str) -> str:
        return self._wrap("32", text)

    def bad(self, text: str) -> str:
        return self._wrap("31", text)

    def warn(self, text: str) -> str:
        return self._wrap("33", text)

    def quiet(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)


def style(stream: TextIO) -> Style:
    return Style(color=stream.isatty() and "NO_COLOR" not in os.environ)


class Output:
    """Everything a command prints goes through here, so ``--json`` and ``NO_COLOR`` hold for
    every command."""

    def __init__(self, *, as_json: bool = False, stream: TextIO | None = None) -> None:
        self.as_json = as_json
        self.stream = stream or sys.stdout
        self.s = style(self.stream)

    def line(self, text: str = "") -> None:
        if not self.as_json:
            print(text, file=self.stream)

    def data(self, value: Any) -> None:
        """The command's result for scripts (only with --json)."""
        if self.as_json:
            print(json.dumps(value, indent=2, sort_keys=True, default=str), file=self.stream)

    def header(self, text: str) -> None:
        self.line()
        self.line(f"  {self.s.bold(text)}")
        self.line()

    def ok(self, text: str, detail: str = "") -> None:
        self.line(f"  {self.s.good('✓')} {_pad(text, 28)}{self.s.quiet(detail)}")

    def fail(self, text: str, *details: str) -> None:
        self.line(f"  {self.s.bad('✗')} {text}")
        for detail in details:
            self.line(f"    {detail}")

    def hint(self, text: str) -> None:
        self.line(f"  {self.s.quiet(text)}")

    def table(self, header: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
        rows = [list(r) for r in rows]
        if self.as_json:
            return
        widths = [len(h) for h in header]
        for row in rows:
            for i, cell in enumerate(row[: len(header)]):
                widths[i] = max(widths[i], len(_plain(cell)))
        terminal = shutil.get_terminal_size((100, 20)).columns
        titles = "   ".join(_pad(h, w) for h, w in zip(header, widths, strict=True))
        self.line("  " + self.s.quiet(titles.rstrip()))
        for row in rows:
            text = (
                "  " + "   ".join(_pad(c, w) for c, w in zip(row, widths, strict=False))
            ).rstrip()
            if len(_plain(text)) > terminal:  # cut the plain text: never inside a colour code
                text = _plain(text)[: terminal - 1] + "…"
            self.line(text)

    def step(self, step: dict[str, Any]) -> None:
        """One step of a rollout, as `neura deploy` follows it."""
        mark = {
            "passed": self.s.good(MARKS["passed"]),
            "failed": self.s.bad(MARKS["failed"]),
            "running": self.s.warn(MARKS["running"]),
            "skipped": self.s.quiet(MARKS["skipped"]),
        }.get(step["state"], " ")
        label = STEP_LABELS.get(step["name"], step["name"])
        summary = step.get("summary", "")
        text = label if step["state"] != "pending" else self.s.quiet(label)
        self.line(f"  {mark} {_pad(text, 22)}{self.s.quiet(summary) if summary else ''}")


def _plain(text: str) -> str:
    """``text`` without colour codes, for measuring."""
    out, skip = [], False
    for char in text:
        if char == "\033":
            skip = True
        elif skip and char == "m":
            skip = False
        elif not skip:
            out.append(char)
    return "".join(out)


def _pad(text: str, width: int) -> str:
    """``text`` padded to ``width``; a longer one keeps two spaces before what follows."""
    length = len(_plain(text))
    return text + " " * (width - length if length <= width else 2)


def when(timestamp: str | None) -> str:
    if not timestamp:
        return ""
    return timestamp.replace("T", " ")[:16]
