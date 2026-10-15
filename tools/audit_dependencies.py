"""Audit every locked dependency for known vulnerabilities (design §18.2, supply chain).

    python tools/audit_dependencies.py

- Python: every package in uv.lock, all groups (PyTorch and LeRobot included), with pip-audit
  against the PyPI vulnerability database.
- JavaScript: the console's and the docs site's pnpm lockfiles, with `pnpm audit`.

Each finding must be fixed or listed, with its reason, in security/vulnerability-exceptions.yaml.
The audit fails on any other finding and on an exception past its `expires` date, and reports
exceptions that no longer match anything so the file stays honest.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXCEPTIONS = ROOT / "security" / "vulnerability-exceptions.yaml"
PIP_AUDIT = "pip-audit==2.10.1"
NPM_PROJECTS = ("console", "site")


@dataclass(frozen=True, slots=True)
class Finding:
    ecosystem: str
    project: str
    package: str
    version: str
    ids: frozenset[str]
    summary: str

    def describe(self) -> str:
        return (
            f"{self.ecosystem} {self.project}: {self.package} {self.version} "
            f"({', '.join(sorted(self.ids))}) {self.summary}"
        )


def python_findings() -> list[Finding]:
    with tempfile.TemporaryDirectory() as tmp:
        requirements = Path(tmp) / "requirements.txt"
        report = Path(tmp) / "audit.json"
        subprocess.run(
            [
                "uv", "export", "--frozen", "--no-hashes", "--no-emit-workspace",
                "--all-packages", "--all-groups", "--format", "requirements-txt",
                "-o", str(requirements),
            ],
            cwd=ROOT, check=True, capture_output=True,
        )  # fmt: skip
        subprocess.run(
            [
                "uvx", "--from", PIP_AUDIT, "pip-audit", "-r", str(requirements),
                "--no-deps", "--disable-pip", "--progress-spinner", "off",
                "-f", "json", "-o", str(report),
            ],
            cwd=ROOT, check=False, capture_output=True,
        )  # fmt: skip
        if not report.exists():
            raise SystemExit("pip-audit produced no report")
        data = json.loads(report.read_text(encoding="utf-8"))
    findings = []
    for dependency in data.get("dependencies", []):
        for vuln in dependency.get("vulns", []):
            findings.append(
                Finding(
                    "python",
                    "workspace",
                    dependency["name"].lower(),
                    dependency.get("version", "?"),
                    frozenset([vuln["id"], *vuln.get("aliases", [])]),
                    f"fixed in {', '.join(vuln.get('fix_versions') or ['nothing yet'])}",
                )
            )
    return findings


def npm_findings(project: str) -> list[Finding]:
    result = subprocess.run(
        ["pnpm", "audit", "--json"], cwd=ROOT / project, check=False, capture_output=True, text=True
    )
    try:
        data: dict[str, Any] = json.loads(result.stdout)
    except ValueError as exc:
        raise SystemExit(f"pnpm audit in {project} failed: {result.stderr[:500]}") from exc
    findings = []
    for advisory in (data.get("advisories") or {}).values():
        versions = sorted({f["version"] for f in advisory.get("findings", [])})
        findings.append(
            Finding(
                "npm",
                project,
                advisory["module_name"].lower(),
                ", ".join(versions),
                frozenset([advisory["github_advisory_id"], *advisory.get("cves", [])]),
                f"{advisory['severity']}: {advisory['title']}",
            )
        )
    return findings


def main() -> int:
    exceptions: list[dict[str, Any]] = (
        yaml.safe_load(EXCEPTIONS.read_text(encoding="utf-8")) or {}
    ).get("exceptions") or []
    today = datetime.date.today()
    findings = python_findings()
    for project in NPM_PROJECTS:
        findings.extend(npm_findings(project))
    problems: list[str] = []
    used: set[int] = set()
    for finding in findings:
        match = next(
            (
                i
                for i, e in enumerate(exceptions)
                if e["ecosystem"] == finding.ecosystem
                and e["package"] == finding.package
                and e["id"] in finding.ids
                and e.get("project", finding.project) == finding.project
            ),
            None,
        )
        if match is None:
            problems.append(f"vulnerable: {finding.describe()}")
            continue
        used.add(match)
        expires = exceptions[match]["expires"]
        if not isinstance(expires, datetime.date) or expires < today:
            problems.append(f"exception expired ({expires}): {finding.describe()}")
        else:
            print(f"accepted until {expires}: {finding.describe()}", file=sys.stderr)
    for i, entry in enumerate(exceptions):
        if i not in used:
            print(f"note: exception {entry['id']} ({entry['package']}) matches nothing; remove it")
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"audited: {len(findings)} findings, {len(problems)} not accepted", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
