"""Generate the outbound schema module from catalog/outbound.yaml (design §18.2).

Usage:
    python tools/gen_outbound.py           # write the generated module
    python tools/gen_outbound.py --check   # exit 1 if the generated module is stale
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "outbound.yaml"
OUTPUT = ROOT / "packages" / "neura-contracts" / "src" / "neura_contracts" / "outbound_schema.py"

KINDS = {"agent", "enroll", "fetch_sealed_weights"}
PATH = re.compile(r"^[a-z][a-z0-9_]*(\.([a-z][a-z0-9_]*|\*))*$")
METRIC = re.compile(r"^[a-z][a-z0-9_]*$")


class CatalogError(Exception):
    pass


def _fields(entries: Any, where: str) -> dict[str, tuple[bool, str]]:
    if not isinstance(entries, dict) or not entries:
        raise CatalogError(f"{where} must be a non-empty mapping")
    out: dict[str, tuple[bool, str]] = {}
    for path, entry in entries.items():
        if not isinstance(path, str) or not PATH.match(path):
            raise CatalogError(f"{where}: invalid path {path!r}")
        if not isinstance(entry, dict) or set(entry) != {"minimal", "description"}:
            raise CatalogError(f"{where}.{path}: needs exactly minimal and description")
        if not isinstance(entry["minimal"], bool):
            raise CatalogError(f"{where}.{path}: minimal must be a boolean")
        description = entry["description"]
        if not isinstance(description, str) or not description.strip():
            raise CatalogError(f"{where}.{path}: description is required")
        out[path] = (entry["minimal"], description.strip())
    return out


def load_catalog(path: Path = CATALOG) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "messages",
        "results",
        "metrics",
    }:
        raise CatalogError("expected schema_version, messages, results and metrics")
    if not isinstance(data["schema_version"], int) or data["schema_version"] < 1:
        raise CatalogError("schema_version must be a positive integer")
    messages = data["messages"]
    if not isinstance(messages, dict) or set(messages) != KINDS:
        raise CatalogError(f"messages must describe exactly {sorted(KINDS)}")
    metrics = data["metrics"]
    if not isinstance(metrics, dict) or not metrics:
        raise CatalogError("metrics must be a non-empty mapping")
    parsed_metrics: dict[str, tuple[tuple[str, ...], str, str]] = {}
    for name, entry in metrics.items():
        if not METRIC.match(str(name)):
            raise CatalogError(f"invalid metric name {name!r}")
        if not isinstance(entry, dict) or set(entry) != {"labels", "unit", "description"}:
            raise CatalogError(f"metrics.{name}: needs exactly labels, unit and description")
        labels = entry["labels"]
        if not isinstance(labels, list) or not all(METRIC.match(str(label)) for label in labels):
            raise CatalogError(f"metrics.{name}: labels must be a list of names")
        parsed_metrics[name] = (tuple(labels), str(entry["unit"]), str(entry["description"]))
    return {
        "schema_version": data["schema_version"],
        "messages": {kind: _fields(messages[kind], f"messages.{kind}") for kind in sorted(KINDS)},
        "results": _fields(data["results"], "results"),
        "metrics": parsed_metrics,
    }


def render(catalog: dict[str, Any]) -> str:
    lines = [
        '"""What a site agent may send to the control plane (design §18.2).',
        "",
        "GENERATED FILE: do not edit. Source: catalog/outbound.yaml.",
        "Regenerate with `make gen`.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class OutboundField:",
        "    minimal: bool",
        "    description: str",
        "",
        "",
        "@dataclass(frozen=True, slots=True)",
        "class OutboundMetric:",
        "    labels: tuple[str, ...]",
        "    unit: str",
        "    description: str",
        "",
        "",
        f"SCHEMA_VERSION = {catalog['schema_version']}",
        "",
        "MESSAGES: dict[str, dict[str, OutboundField]] = {",
    ]
    for kind, fields in catalog["messages"].items():
        lines.append(f"    {kind!r}: {{")
        for path, (minimal, description) in fields.items():
            lines.append(f"        {path!r}: OutboundField({minimal}, {description!r}),")
        lines.append("    },")
    lines += ["}", "", "RESULTS: dict[str, OutboundField] = {"]
    for path, (minimal, description) in catalog["results"].items():
        lines.append(f"    {path!r}: OutboundField({minimal}, {description!r}),")
    lines += ["}", "", "METRICS: dict[str, OutboundMetric] = {"]
    for name, (labels, unit, description) in catalog["metrics"].items():
        lines.append(f"    {name!r}: OutboundMetric({labels!r}, {unit!r}, {description!r}),")
    lines += ["}", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if output is stale")
    args = parser.parse_args()
    try:
        content = render(load_catalog())
    except CatalogError as exc:
        print(f"outbound catalog error: {exc}", file=sys.stderr)
        return 1
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != content:
            print(f"{OUTPUT.relative_to(ROOT)} is stale; run `make gen`", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
