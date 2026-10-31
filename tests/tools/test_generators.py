"""Generated files must match their sources, and the reason-code catalog must be well formed."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import gen_reason_codes  # noqa: E402


@pytest.mark.parametrize(
    "script",
    [
        "gen_reason_codes.py",
        "gen_fleet.py",
        "gen_proto.py",
        "gen_schemas.py",
        "refresh_example_bindings.py",
        "codemap.py",
    ],
)
def test_generated_files_are_current(script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / script), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "code": "AGENT_EXAMPLE",
        "area": "agent",
        "severity": "error",
        "retryable": False,
        "message": "An example.",
    }
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    "entry",
    [
        _entry(code="lowercase_code"),
        _entry(code="NOSEPARATOR"),
        _entry(area="cloud"),
        _entry(severity="critical"),
        _entry(retryable="no"),
        _entry(message="no period"),
        _entry(http_status=500),
        _entry(area="api", code="API_EXAMPLE"),
        _entry(area="api", code="API_EXAMPLE", http_status=200),
        _entry(extra="field"),
    ],
)
def test_catalog_rejects_malformed_entries(entry: dict[str, Any]) -> None:
    with pytest.raises(gen_reason_codes.CatalogError):
        gen_reason_codes.validate_entry(entry, set())


def test_catalog_rejects_duplicates() -> None:
    seen: set[str] = set()
    gen_reason_codes.validate_entry(_entry(), seen)
    with pytest.raises(gen_reason_codes.CatalogError):
        gen_reason_codes.validate_entry(_entry(), seen)


def test_catalog_loads() -> None:
    entries = gen_reason_codes.load_catalog()
    assert len(entries) == len({e["code"] for e in entries})
