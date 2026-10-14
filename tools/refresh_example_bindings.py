"""Refresh execution-profile bindings in examples/contracts to the current document digests.

Each example profile names the files it binds to in the labels ``example-artifact-file`` and
``example-robot-file``.

Usage:
    python tools/refresh_example_bindings.py           # rewrite bindings in place
    python tools/refresh_example_bindings.py --check   # exit 1 if any binding is stale
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from neura_contracts import ExecutionProfile, load_document

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "contracts"


def _digest_of(name: str) -> str:
    return load_document((EXAMPLES / name).read_text(encoding="utf-8")).digest()


def refresh(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    profile = load_document(text)
    if not isinstance(profile, ExecutionProfile):
        raise SystemExit(f"{path.name}: not an ExecutionProfile")
    labels = profile.metadata.labels
    artifact = _digest_of(labels["example-artifact-file"])
    robot = _digest_of(labels["example-robot-file"])
    updated = re.sub(r"(model_artifact: )sha256:[0-9a-f]{64}", rf"\g<1>{artifact}", text, count=1)
    updated = re.sub(r"(robot_profile: )sha256:[0-9a-f]{64}", rf"\g<1>{robot}", updated, count=1)
    return text, updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for path in sorted(EXAMPLES.glob("*.profile.yaml")):
        before, after = refresh(path)
        if before != after:
            stale.append(path.name)
            if not args.check:
                path.write_text(after, encoding="utf-8")
    if args.check and stale:
        print(f"stale example bindings: {', '.join(stale)}; run `make examples`", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
