"""The docs site's checks (design §26.7): labels, wording, generated references, quickstart."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import check_docs  # noqa: E402


def _pages_in(directory: Path) -> list[check_docs.Page]:
    original = check_docs.DOCS
    check_docs.DOCS = directory
    try:
        return list(check_docs.pages())
    finally:
        check_docs.DOCS = original


def _problems(tmp_path: Path, text: str) -> list[str]:
    (tmp_path / "page.md").write_text(text, encoding="utf-8")
    report = check_docs.Report()
    original_root, original_docs = check_docs.ROOT, check_docs.DOCS
    check_docs.ROOT, check_docs.DOCS = tmp_path, tmp_path
    try:
        for page in check_docs.pages():
            check_docs.check_labels(page, report)
            check_docs.check_wording(page, report)
    finally:
        check_docs.ROOT, check_docs.DOCS = original_root, original_docs
    return report.problems


def test_the_site_passes_its_checks() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_docs.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_reference_pages_are_current() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gen_docs.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("front_matter", "ok"),
    [
        ("status: Experimental", True),
        ("status: Proposed", True),
        ("status: Supported", True),
        ("status: Deprecated\nremoval: 2027-01-31", True),
        ("status: Deprecated", False),
        ("status: Beta", False),
        ("title: No label", False),
    ],
)
def test_every_page_needs_one_valid_label(tmp_path: Path, front_matter: str, ok: bool) -> None:
    problems = _problems(tmp_path, f"---\n{front_matter}\n---\n\n# Page\n")
    assert (problems == []) is ok, problems


@pytest.mark.parametrize(
    "sentence",
    [
        "Neura supports ACT on the SO-101.",
        "ACT is supported on the SO-101.",
        "Delivery is guaranteed.",
        "It is production-ready.",
        "An enterprise grade platform.",
    ],
)
def test_unqualified_claims_are_refused_unless_supported(tmp_path: Path, sentence: str) -> None:
    assert _problems(tmp_path, f"---\nstatus: Experimental\n---\n\n{sentence}\n")
    assert _problems(tmp_path, f"---\nstatus: Proposed\n---\n\n{sentence}\n")
    assert not _problems(tmp_path, f"---\nstatus: Supported\n---\n\n{sentence}\n")


def test_code_labels_and_generated_pages_are_not_claims(tmp_path: Path) -> None:
    text = (
        "---\nstatus: Experimental\n---\n\n"
        "A combination is labeled **Supported** after qualification. Ask support for access.\n\n"
        "`--supports-x` in inline code is fine.\n\n"
        "```text\nThe field is not supported by this release.\n```\n"
    )
    assert _problems(tmp_path, text) == []
    generated = (
        "---\nstatus: Experimental\ngenerated: true\n---\n\nNot supported by this release.\n"
    )
    assert _problems(tmp_path, generated) == []


def test_quickstart_runs_only_marked_blocks_in_order(tmp_path: Path) -> None:
    (tmp_path / "quickstart.md").write_text(
        "---\nstatus: Experimental\n---\n\n"
        "```bash\ngit clone x\n```\n\n"
        "```bash quickstart\necho one\n```\n\n"
        "```python\nprint('no')\n```\n\n"
        "```bash quickstart\necho two\n```\n",
        encoding="utf-8",
    )
    page = _pages_in(tmp_path)[0]
    script = check_docs.quickstart_script(page)
    assert script == "set -euo pipefail\necho one\n\necho two\n"
