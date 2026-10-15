"""Checks for the developer docs site (design §26.2, §26.7).

    python tools/check_docs.py                    # labels, wording, supported data
    python tools/check_docs.py --external         # also every external link answers
    python tools/check_docs.py --run-quickstart   # also run the quickstart's commands

The Docusaurus build itself fails on broken internal links, anchors and images; this script
checks what the build cannot:

- Every page has exactly one valid status label in its front matter (Deprecated with a removal
  date).
- Wording: a page not labeled Supported makes no unqualified claim. Outside code it may not say
  "supports", "supported", "guarantees", "production-ready" and the like; generated reference
  pages, which quote product strings, are exempt.
- The Supported table's data (site/data/supported.json) has every field and links its evidence.
- With --external, every http(s) link outside code answers; links into this repository must
  name a file or directory that exists in the checkout.
- With --run-quickstart, the shell blocks of the quickstart marked ``quickstart`` run, in order,
  in one shell, from the repository root, and must succeed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "site" / "docs"
SUPPORTED = ROOT / "site" / "data" / "supported.json"
LABELS = ("Proposed", "Experimental", "Supported", "Deprecated")

# Unqualified claims a page not labeled Supported may not make (design §26.2). Lowercase
# "supported" only: "Supported" with a capital is the label's own name.
CLAIMS = [
    (re.compile(r"\bsupports\b"), "supports"),
    (re.compile(r"\bsupported\b"), "supported"),
    (re.compile(r"\bguarantee[sd]?\b", re.IGNORECASE), "guarantees"),
    (re.compile(r"\bproduction[- ](ready|grade)\b", re.IGNORECASE), "production-ready"),
    (re.compile(r"\benterprise[- ]grade\b", re.IGNORECASE), "enterprise-grade"),
    (re.compile(r"\bbattle[- ]tested\b", re.IGNORECASE), "battle-tested"),
    (re.compile(r"\bbullet[- ]?proof\b", re.IGNORECASE), "bulletproof"),
]
FENCE = re.compile(r"^(```+|~~~+)(.*)$")
INLINE_CODE = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"\]\((https?://[^)\s]+)\)|<(https?://[^>\s]+)>")
SUPPORTED_FIELDS = ("model", "robot", "execution_profile", "gpu", "qualified_on", "evidence")
LOCAL_HOSTS = ("127.0.0.1", "localhost", ".example", ".invalid")
# Links into this repository are checked against the checkout: the docs and the code they link
# to change in the same commit (and the repository may not be public).
REPO_LINK = re.compile(r"^https://github\.com/rkd0608/neura/(?:blob|tree|edit)/main/(.+)$")


@dataclass(slots=True)
class Page:
    path: Path
    meta: dict[str, Any]
    body: str
    body_line: int  # line number of the body's first line

    @property
    def name(self) -> str:
        return str(self.path.relative_to(ROOT))


@dataclass(slots=True)
class Report:
    problems: list[str] = field(default_factory=list)

    def add(self, problem: str) -> None:
        self.problems.append(problem)


def pages() -> Iterator[Page]:
    for path in sorted(DOCS.rglob("*.md*")):
        if path.suffix not in (".md", ".mdx"):
            continue
        text = path.read_text(encoding="utf-8")
        meta: dict[str, Any] = {}
        body, body_line = text, 1
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end != -1:
                loaded = yaml.safe_load(text[4:end])
                meta = loaded if isinstance(loaded, dict) else {}
                body = text[end + 5 :]
                body_line = text[: end + 5].count("\n") + 1
        yield Page(path, meta, body, body_line)


def prose_lines(body: str) -> Iterator[tuple[int, str]]:
    """Lines outside fenced code, with inline code removed (0-based offsets)."""
    fence: str | None = None
    for offset, line in enumerate(body.splitlines()):
        match = FENCE.match(line.strip())
        if match:
            marker = match.group(1)[0] * 3
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is None:
            yield offset, INLINE_CODE.sub("", line)


def check_labels(page: Page, report: Report) -> None:
    status = page.meta.get("status")
    if status not in LABELS:
        report.add(f"{page.name}: front matter needs `status:` with one of {', '.join(LABELS)}")
        return
    if status == "Deprecated":
        removal = page.meta.get("removal")
        if not isinstance(removal, datetime.date):
            report.add(f"{page.name}: a Deprecated page needs `removal: YYYY-MM-DD`")


def check_wording(page: Page, report: Report) -> None:
    if page.meta.get("status") == "Supported" or page.meta.get("generated") is True:
        return
    for offset, line in prose_lines(page.body):
        for pattern, word in CLAIMS:
            if pattern.search(line):
                report.add(
                    f"{page.name}:{page.body_line + offset}: '{word}' is an unqualified claim on "
                    f"a page labeled {page.meta.get('status')}; say what was tested instead"
                )


def check_supported(report: Report) -> None:
    try:
        rows = json.loads(SUPPORTED.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.add(f"{SUPPORTED.relative_to(ROOT)}: {exc}")
        return
    if not isinstance(rows, list):
        report.add(f"{SUPPORTED.relative_to(ROOT)}: must be a list")
        return
    for i, row in enumerate(rows):
        missing = [f for f in SUPPORTED_FIELDS if not isinstance(row, dict) or not row.get(f)]
        if missing:
            report.add(f"{SUPPORTED.relative_to(ROOT)}[{i}]: missing {', '.join(missing)}")
        elif not str(row["evidence"]).startswith("https://"):
            report.add(f"{SUPPORTED.relative_to(ROOT)}[{i}]: evidence must be an https link")


def external_links(all_pages: list[Page]) -> dict[str, list[str]]:
    links: dict[str, list[str]] = {}
    for page in all_pages:
        for offset, line in prose_lines(page.body):
            for match in LINK.finditer(line):
                url = match.group(1) or match.group(2)
                host = re.sub(r"^https?://", "", url).split("/", 1)[0]
                if any(host == h or host.endswith(h) for h in LOCAL_HOSTS):
                    continue
                links.setdefault(url, []).append(f"{page.name}:{page.body_line + offset}")
    return links


def check_external(all_pages: list[Page], report: Report) -> None:
    import httpx

    with httpx.Client(
        follow_redirects=True, timeout=20, headers={"User-Agent": "neura-docs"}
    ) as http:
        for url, where in sorted(external_links(all_pages).items()):
            if match := REPO_LINK.match(url):
                if not (ROOT / match.group(1).split("#", 1)[0]).exists():
                    report.add(f"{where[0]}: {url} -> no {match.group(1)} in this repository")
                continue
            try:
                response = http.head(url)
                if response.status_code in (403, 405, 501):
                    response = http.get(url)
                ok = response.status_code < 400
                detail = str(response.status_code)
            except httpx.HTTPError as exc:
                ok, detail = False, type(exc).__name__
            if not ok:
                report.add(f"{where[0]}: {url} -> {detail}")


def quickstart_script(page: Page) -> str:
    """The page's ```bash quickstart blocks, in order, as one script."""
    blocks: list[str] = []
    current: list[str] = []
    in_block = capture = False
    for line in page.body.splitlines():
        match = FENCE.match(line.strip())
        if match:
            if not in_block:
                info = match.group(2).split()
                in_block, capture, current = True, info[:1] == ["bash"] and "quickstart" in info, []
            else:
                if capture:
                    blocks.append("\n".join(current))
                in_block = capture = False
            continue
        if capture:
            current.append(line)
    return "set -euo pipefail\n" + "\n\n".join(blocks) + "\n"


def run_quickstart(report: Report, timeout_s: float) -> None:
    page = next(p for p in pages() if p.path.name == "quickstart.md")
    script = quickstart_script(page)
    if script.count("\n") < 3:
        report.add(f"{page.name}: no ```bash quickstart blocks to run")
        return
    print(script, flush=True)
    try:
        result = subprocess.run(  # our own documented commands
            ["bash", "-c", script],
            cwd=ROOT,
            timeout=timeout_s,
            check=False,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except subprocess.TimeoutExpired:
        report.add(f"{page.name}: the quickstart did not finish in {timeout_s:.0f} s")
        return
    if result.returncode != 0:
        report.add(f"{page.name}: the quickstart failed with exit status {result.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--external", action="store_true", help="check external links too")
    parser.add_argument("--run-quickstart", action="store_true", help="run the quickstart")
    parser.add_argument("--quickstart-timeout", type=float, default=900.0)
    args = parser.parse_args()
    report = Report()
    all_pages = list(pages())
    if not all_pages:
        report.add(f"no pages under {DOCS.relative_to(ROOT)}")
    for page in all_pages:
        check_labels(page, report)
        check_wording(page, report)
    check_supported(report)
    if args.external:
        check_external(all_pages, report)
    if args.run_quickstart:
        run_quickstart(report, args.quickstart_timeout)
    for problem in report.problems:
        print(problem, file=sys.stderr)
    if report.problems:
        return 1
    print(f"docs: {len(all_pages)} pages checked", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
