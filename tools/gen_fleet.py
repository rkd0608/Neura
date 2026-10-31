"""Generate the fleet's words (Python for the CLI, TypeScript for the console) from
catalog/fleet.yaml, checking every state, step and kind it names against api/openapi.yaml.

Usage:
    python tools/gen_fleet.py           # write the generated modules
    python tools/gen_fleet.py --check   # exit 1 if a generated module is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "fleet.yaml"
SPEC = ROOT / "api" / "openapi.yaml"
OUTPUT = ROOT / "packages" / "neura" / "src" / "neura_cli" / "fleet.py"
TS_OUTPUT = ROOT / "console" / "src" / "generated" / "fleet.ts"

TONES = ("ok", "warn", "danger", "info", "muted")
# The robot status fields a rule may test besides its phase, with their TypeScript types.
FIELDS = {"discovered": "boolean", "present": "boolean", "policy_version_id": "string"}
# The sections that must list exactly what the API defines, in its order.
FROM_API = ("rollout_states", "rollout_steps", "step_states", "rollout_kinds")
SECTIONS = {"catalog_version", "robot_states", "robot_rules", *FROM_API}


class CatalogError(Exception):
    pass


def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def api_enums(spec: dict[str, Any]) -> dict[str, list[str]]:
    schemas = spec["components"]["schemas"]
    step = schemas["RolloutStep"]["properties"]
    return {
        "phase": schemas["ConsolePhase"]["enum"],
        "rollout_states": schemas["Rollout"]["properties"]["status"]["properties"]["state"]["enum"],
        "rollout_steps": step["name"]["enum"],
        "step_states": step["state"]["enum"],
        "rollout_kinds": schemas["RolloutSpec"]["properties"]["kind"]["enum"],
        "robot_fields": sorted(schemas["Robot"]["properties"]["status"]["properties"]),
    }


def load_catalog(path: Path = CATALOG, spec_path: Path = SPEC) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate(data, api_enums(yaml.safe_load(spec_path.read_text(encoding="utf-8"))))
    return dict(data)


def validate(data: Any, enums: dict[str, list[str]]) -> None:
    if not isinstance(data, dict) or data.get("catalog_version") != 1:
        raise CatalogError("catalog_version must be 1")
    if set(data) != SECTIONS:
        raise CatalogError(f"the catalog has exactly these sections: {sorted(SECTIONS)}")
    states = data["robot_states"]
    for state, tone in states.items():
        _tone(f"robot state {state!r}", tone)
    rules = data["robot_rules"]
    if not rules or rules[-1].get("when") != {}:
        raise CatalogError("the last robot rule has no conditions: it decides every other robot")
    for rule in rules:
        _rule(rule, states, enums, last=rule is rules[-1])
    undecided = set(states) - {rule["state"] for rule in rules}
    if undecided:
        raise CatalogError(f"no rule decides the robot states {sorted(undecided)}")
    for section in FROM_API:
        if list(data[section]) != enums[section]:
            raise CatalogError(f"{section} must be the API's, in its order: {enums[section]}")
    for state, entry in data["rollout_states"].items():
        if set(entry) - {"label", "tone", "terminal"} or not isinstance(entry.get("label"), str):
            raise CatalogError(f"rollout state {state!r}: a label, a tone and maybe terminal")
        _tone(f"rollout state {state!r}", entry.get("tone"))
    for state, entry in data["step_states"].items():
        if set(entry) != {"tone", "mark"} or len(str(entry["mark"])) != 1:
            raise CatalogError(f"step state {state!r}: a tone and a one-character mark")
        _tone(f"step state {state!r}", entry["tone"])


def _rule(rule: Any, states: dict[str, str], enums: dict[str, list[str]], *, last: bool) -> None:
    if not isinstance(rule, dict) or rule.get("state") not in states:
        raise CatalogError(f"rule {rule!r} names a state robot_states does not have")
    conditions = rule.get("when")
    if not isinstance(conditions, dict) or (not conditions and not last):
        raise CatalogError(f"rule {rule!r} needs conditions")
    for field, expected in conditions.items():
        if field == "phase":
            if not expected or set(expected) - set(enums["phase"]):
                raise CatalogError(f"rule {rule!r}: phases are among {enums['phase']}")
        elif field not in FIELDS or field not in enums["robot_fields"]:
            raise CatalogError(f"rule {rule!r}: a rule cannot test {field!r}")
        elif not isinstance(expected, bool):
            raise CatalogError(f"rule {rule!r}: {field} is true (set) or false")


def _tone(what: str, tone: Any) -> None:
    if tone not in TONES:
        raise CatalogError(f"{what}: the tone is one of {TONES}")


def _condition(field: str, expected: Any, *, python: bool) -> str:
    if field == "phase":
        if python:
            if len(expected) == 1:
                return f"phase == {_q(expected[0])}"
            return f"phase in ({', '.join(_q(p) for p in expected)})"
        either = " || ".join(f"phase === {_q(p)}" for p in expected)
        return either if len(expected) == 1 else f"({either})"
    if python:
        return f"status.get({_q(field)})" + ("" if expected else " is False")
    return f"status.{field}" + ("" if expected else " === false")


def _rules(rules: list[dict[str, Any]], *, python: bool) -> list[str]:
    lines = []
    for rule in rules[:-1]:
        joined = (" and " if python else " && ").join(
            _condition(field, expected, python=python) for field, expected in rule["when"].items()
        )
        if python:
            lines += [f"    if {joined}:", f"        return {_q(rule['state'])}"]
        else:
            lines.append(f"  if ({joined}) return {_q(rule['state'])};")
    final = _q(rules[-1]["state"])
    return [*lines, f"    return {final}" if python else f"  return {final};"]


def _table(name: str, key: str, value: str, rows: dict[str, str], *, python: bool) -> list[str]:
    if python:
        head, row, tail = f"{name}: dict[{key}, {value}] = {{", "    {}: {},", "}"
    else:
        head = f"export const {name}: Readonly<Record<{key}, {value}>> = {{"
        row, tail = "  {}: {},", "};"
    return [head, *(row.format(_q(k), _q(v)) for k, v in rows.items()), tail, ""]


def _tables(data: dict[str, Any]) -> list[tuple[str, str, bool, dict[str, str]]]:
    """Each table of rollout words: its name, its key's TypeScript type, whether its values are
    tones, and its rows."""
    rollouts, steps = data["rollout_states"], data["step_states"]
    return [
        ("ROLLOUT_LABELS", "RolloutState", False, {k: v["label"] for k, v in rollouts.items()}),
        ("ROLLOUT_TONES", "RolloutState", True, {k: v["tone"] for k, v in rollouts.items()}),
        ("STEP_LABELS", "StepName", False, dict(data["rollout_steps"])),
        ("STEP_TONES", "StepState", True, {k: v["tone"] for k, v in steps.items()}),
        ("STEP_MARKS", "StepState", False, {k: v["mark"] for k, v in steps.items()}),
        ("ROLLOUT_KINDS", "RolloutKind", False, dict(data["rollout_kinds"])),
    ]


def _terminal(data: dict[str, Any]) -> list[str]:
    return [state for state, entry in data["rollout_states"].items() if entry.get("terminal")]


def render(data: dict[str, Any]) -> str:
    states = data["robot_states"]
    lines = [
        '"""The fleet\'s words: what robots and rollouts are called, in the console and from',
        "`neura` (design §19.4, §29).",
        "",
        "GENERATED FILE: do not edit. Source: catalog/fleet.yaml. Regenerate with `make gen`.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from collections.abc import Mapping",
        "from typing import Any, Literal",
        "",
        f"Tone = Literal[{', '.join(_q(t) for t in TONES)}]",
        f"RobotState = Literal[{', '.join(_q(s) for s in states)}]",
        "",
        *_table("ROBOT_TONES", "RobotState", "Tone", states, python=True),
        "",
        "def robot_state(robot: Mapping[str, Any]) -> RobotState:",
        '    """What a robot is doing, in one word: the word the console shows for it."""',
        '    status = robot["status"]',
        '    phase = status.get("phase") or "Unknown"',
        *_rules(data["robot_rules"], python=True),
        "",
        "",
        f"TERMINAL = frozenset({{{', '.join(_q(s) for s in _terminal(data))}}})",
        "",
    ]
    for name, _, tones, rows in _tables(data):
        lines += _table(name, "str", "Tone" if tones else "str", rows, python=True)
    return "\n".join(lines).rstrip("\n") + "\n"


def _union(values: Iterable[str]) -> str:
    return " | ".join(_q(v) for v in values)


def render_typescript(data: dict[str, Any]) -> str:
    states = data["robot_states"]
    lines = [
        "// The fleet's words: what robots and rollouts are called, in the console and from",
        "// `neura` (design §19.4, §29).",
        "// GENERATED FILE: do not edit. Source: catalog/fleet.yaml.",
        "// Regenerate with `make gen`.",
        "",
        f"export type Tone = {_union(TONES)};",
        f"export type RobotState = {_union(states)};",
        f"export type RolloutState = {_union(data['rollout_states'])};",
        f"export type StepName = {_union(data['rollout_steps'])};",
        f"export type StepState = {_union(data['step_states'])};",
        f"export type RolloutKind = {_union(data['rollout_kinds'])};",
        "",
        "/** The fields of a robot's status its state is decided from. */",
        "export interface RobotFacts {",
        "  readonly phase?: string | null;",
        *(f"  readonly {field}?: {kind} | null;" for field, kind in FIELDS.items()),
        "}",
        "",
        *_table("ROBOT_TONES", "RobotState", "Tone", states, python=False),
        "/** What a robot is doing, in one word: the word `neura robots` prints for it. */",
        "export function robotState(robot: { readonly status: RobotFacts }): RobotState {",
        "  const status = robot.status;",
        '  const phase = status.phase ?? "Unknown";',
        *_rules(data["robot_rules"], python=False),
        "}",
        "",
        "export const TERMINAL: ReadonlySet<string> = new Set([",
        *(f"  {_q(s)}," for s in _terminal(data)),
        "]);",
        "",
    ]
    for name, key, tones, rows in _tables(data):
        lines += _table(name, key, "Tone" if tones else "string", rows, python=False)
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if output is stale")
    args = parser.parse_args()
    try:
        data = load_catalog()
    except CatalogError as exc:
        print(f"fleet catalog error: {exc}", file=sys.stderr)
        return 1
    outputs = {OUTPUT: render(data), TS_OUTPUT: render_typescript(data)}
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
