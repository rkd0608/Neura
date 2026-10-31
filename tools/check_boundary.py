"""Keep the open-source packages free of Neura's closed code, so they can be published alone.

    python tools/check_boundary.py

The packages listed under ``[tool.neura] open-source`` in pyproject.toml are published as
``neura-robot`` (design §30). Each may depend on, and import, only the others in that list: any
import of another Neura package fails, including one made inside a function or only for type
checking, since the published package has to install, import and type-check without it. Their
tests are not checked yet: some still drive the closed gateway (§30.2).
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def open_packages() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages: list[str] = config["tool"]["neura"]["open-source"]
    return packages


def modules(package: str) -> set[str]:
    """The top-level import names a workspace package provides."""
    return {p.name for p in (ROOT / "packages" / package / "src").iterdir() if p.is_dir()}


def neura_imports(path: Path) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        found += [(node.lineno, n.split(".")[0]) for n in names if n.startswith("neura")]
    return found


def problems() -> list[str]:
    packages = open_packages()
    allowed = set().union(*(modules(p) for p in packages))
    out = []
    for package in packages:
        manifest = tomllib.loads(
            (ROOT / "packages" / package / "pyproject.toml").read_text(encoding="utf-8")
        )
        for dependency in manifest["project"]["dependencies"]:
            name = dependency.split(">")[0].split("=")[0].split("[")[0].strip()
            if name.startswith("neura") and name not in packages:
                out.append(f"packages/{package}/pyproject.toml: depends on closed {name}")
        for path in sorted((ROOT / "packages" / package / "src").rglob("*.py")):
            for line, module in neura_imports(path):
                if module not in allowed:
                    out.append(f"{path.relative_to(ROOT)}:{line}: imports closed {module}")
    return out


def main() -> int:
    found = problems()
    for line in found:
        print(line, file=sys.stderr)
    if found:
        print(
            "the open-source packages may import only each other (pyproject.toml [tool.neura])",
            file=sys.stderr,
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
