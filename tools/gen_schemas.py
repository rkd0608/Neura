"""Export JSON Schemas for the contract documents into schemas/.

Other languages (the console, partner SDKs) validate documents against these files.

Usage:
    python tools/gen_schemas.py           # write schemas/*.schema.json
    python tools/gen_schemas.py --check   # exit 1 if any schema is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from neura_contracts import API_VERSION, ExecutionProfile, ModelArtifact, RobotProfile

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "schemas"
DOCUMENTS: dict[str, type[BaseModel]] = {
    "model-artifact": ModelArtifact,
    "robot-profile": RobotProfile,
    "execution-profile": ExecutionProfile,
}


def render(name: str) -> str:
    schema = DOCUMENTS[name].model_json_schema(mode="validation")
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:neura:schema:{API_VERSION}:{name}",
        **schema,
    }
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    stale = []
    for name in DOCUMENTS:
        path = OUT / f"{name}.schema.json"
        content = render(name)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != content:
            stale.append(path.name)
            if not args.check:
                path.write_text(content, encoding="utf-8")
    if args.check and stale:
        print(f"stale JSON schemas: {', '.join(stale)}; run `make gen`", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
