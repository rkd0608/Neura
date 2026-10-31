"""Generate the reason-code modules (Python, and TypeScript for the console) from
catalog/reason_codes.yaml.

Usage:
    python tools/gen_reason_codes.py           # write the generated module
    python tools/gen_reason_codes.py --check   # exit 1 if the generated module is stale
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "reason_codes.yaml"
OUTPUT = ROOT / "packages" / "neura-contracts" / "src" / "neura_contracts" / "reason_codes.py"
TS_OUTPUT = ROOT / "console" / "src" / "generated" / "reasonCodes.ts"

AREA_ORDER = (
    "contracts",
    "gateway",
    "scheduler",
    "worker",
    "runtime",
    "agent",
    "capture",
    "evaluation",
    "training",
    "rollout",
    "api",
)
AREAS = set(AREA_ORDER)
SEVERITIES = {"info", "warning", "error", "fatal"}
CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
ALLOWED_KEYS = {"code", "area", "severity", "retryable", "message", "http_status"}


class CatalogError(Exception):
    pass


def load_catalog(path: Path = CATALOG) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("catalog_version") != 1:
        raise CatalogError("catalog_version must be 1")
    entries = data.get("codes")
    if not isinstance(entries, list) or not entries:
        raise CatalogError("catalog must define a non-empty 'codes' list")
    seen: set[str] = set()
    for entry in entries:
        validate_entry(entry, seen)
    return entries


def validate_entry(entry: Any, seen: set[str]) -> None:
    if not isinstance(entry, dict):
        raise CatalogError(f"entry is not a mapping: {entry!r}")
    code = entry.get("code")
    if not isinstance(code, str) or not CODE_PATTERN.match(code):
        raise CatalogError(f"invalid code name: {code!r}")
    if code in seen:
        raise CatalogError(f"duplicate code: {code}")
    seen.add(code)
    unknown = set(entry) - ALLOWED_KEYS
    if unknown:
        raise CatalogError(f"{code}: unknown keys {sorted(unknown)}")
    if entry.get("area") not in AREAS:
        raise CatalogError(f"{code}: area must be one of {sorted(AREAS)}")
    if entry.get("severity") not in SEVERITIES:
        raise CatalogError(f"{code}: severity must be one of {sorted(SEVERITIES)}")
    if not isinstance(entry.get("retryable"), bool):
        raise CatalogError(f"{code}: retryable must be a boolean")
    message = entry.get("message")
    if not isinstance(message, str) or not message.strip().endswith("."):
        raise CatalogError(f"{code}: message must be a sentence ending with a period")
    status = entry.get("http_status")
    if entry["area"] == "api":
        if not isinstance(status, int) or not 400 <= status <= 599:
            raise CatalogError(f"{code}: api codes need an http_status between 400 and 599")
    elif status is not None:
        raise CatalogError(f"{code}: http_status is only allowed for api codes")


def render(entries: list[dict[str, Any]]) -> str:
    lines = [
        '"""Reason codes shared by every Neura component (design §20.5).',
        "",
        "GENERATED FILE: do not edit. Source: catalog/reason_codes.yaml.",
        "Regenerate with `make gen`.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "from neura_contracts.compat import StrEnum",
        "from typing import Literal",
        "",
        "Area = Literal[" + ", ".join(f'"{a}"' for a in AREA_ORDER) + "]",
        'Severity = Literal["info", "warning", "error", "fatal"]',
        "",
        "",
        "class ReasonCode(StrEnum):",
    ]
    lines += [f'    {e["code"]} = "{e["code"]}"' for e in entries]
    lines += [
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class ReasonCodeInfo:",
        "    code: ReasonCode",
        "    area: Area",
        "    severity: Severity",
        "    retryable: bool",
        "    message: str",
        "    http_status: int | None = None",
        "",
        "",
        "REASON_CODES: dict[ReasonCode, ReasonCodeInfo] = {",
    ]
    for e in entries:
        status = e.get("http_status")
        lines += [
            f"    ReasonCode.{e['code']}: ReasonCodeInfo(",
            f"        code=ReasonCode.{e['code']},",
            f'        area="{e["area"]}",',
            f'        severity="{e["severity"]}",',
            f"        retryable={e['retryable']},",
            f"        message={e['message']!r},",
        ]
        if status is not None:
            lines.append(f"        http_status={status},")
        lines.append("    ),")
    lines += [
        "}",
        "",
        "",
        "def info(code: ReasonCode) -> ReasonCodeInfo:",
        '    """Return the catalog entry for a reason code."""',
        "    return REASON_CODES[code]",
        "",
    ]
    return "\n".join(lines)


def render_typescript(entries: list[dict[str, Any]]) -> str:
    """The catalog for the console, which shows each code's plain-language text (§19.1)."""
    lines = [
        "// Reason codes shared by every Neura component (design §20.5).",
        "// GENERATED FILE: do not edit. Source: catalog/reason_codes.yaml.",
        "// Regenerate with `make gen`.",
        "",
        'export type Severity = "info" | "warning" | "error" | "fatal";',
        "",
        "export interface ReasonCodeInfo {",
        "  readonly area: string;",
        "  readonly severity: Severity;",
        "  readonly retryable: boolean;",
        "  readonly message: string;",
        "}",
        "",
        "export const REASON_CODES: Readonly<Record<string, ReasonCodeInfo>> = {",
    ]
    for e in entries:
        message = e["message"].replace("\\", "\\\\").replace('"', '\\"')
        lines += [
            f"  {e['code']}: {{",
            f'    area: "{e["area"]}",',
            f'    severity: "{e["severity"]}",',
            f"    retryable: {str(e['retryable']).lower()},",
            f'    message: "{message}",',
            "  },",
        ]
    lines += ["};", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if output is stale")
    args = parser.parse_args()
    try:
        entries = load_catalog()
    except CatalogError as exc:
        print(f"reason-code catalog error: {exc}", file=sys.stderr)
        return 1
    outputs = {OUTPUT: render(entries), TS_OUTPUT: render_typescript(entries)}
    if args.check:
        stale = [
            path
            for path, content in outputs.items()
            if (path.read_text(encoding="utf-8") if path.exists() else "") != content
        ]
        for path in stale:
            print(f"{path.relative_to(ROOT)} is stale; run `make gen`", file=sys.stderr)
        return 1 if stale else 0
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
