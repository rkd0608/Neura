"""What is running on this machine and what went wrong, in one report: read it before guessing.

    python tools/dev_report.py              # print it and write .verify/dev-report.md
    python tools/dev_report.py --lines 300  # keep more of each log

It collects the dev stack's containers (`make dev`) with the end of each one's log; what answers
on the usual ports (the control plane's /readyz, a site agent's /v1/local/status, a data plane's
/v1/local/dataplane), and the control plane the `neura` command is signed in to; every site
`neura site join` started on this machine (~/.local/share/neura/sites), with its process and the
end of its agent.log; and whether Postgres answers. Errors, critical lines and tracebacks from
all of those logs come first, each with where it came from: the first thing read is the first
thing that went wrong, with its stack.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".verify" / "dev-report.md"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "deploy" / "compose" / "compose.yaml")]
ENDPOINTS = {
    "control plane on :8080 (the dev stack's), ready": "http://127.0.0.1:8080/readyz",
    "site agent on :8090": "http://127.0.0.1:8090/v1/local/status",
    "data plane on :8091": "http://127.0.0.1:8091/v1/local/dataplane",
}
FAILURE = re.compile(r"\b(ERROR|CRITICAL|FATAL|Traceback|panic|Segmentation fault|Killed)\b")


@dataclass(slots=True)
class Report:
    sections: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def add(self, title: str, body: str) -> None:
        self.sections.append(f"## {title}\n\n{body.rstrip()}\n")

    def scan(self, source: str, text: str) -> None:
        """Pull failures out of a log: each error line, and each traceback whole."""
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if "Traceback (most recent call last)" in line:
                end = i + 1
                while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end]):
                    end += 1
                block = "\n".join(lines[i : min(end + 1, len(lines))])
                self.failures.append(f"{source}:\n```\n{block}\n```")
                i = end + 1
                continue
            if FAILURE.search(line):
                self.failures.append(f"{source}: `{line.strip()[:400]}`")
            i += 1


def probe(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - loopback only
            body = response.read(4000).decode("utf-8", "replace")
            return f"HTTP {response.status}: {compact(body)}"
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}: {compact(error.read(2000).decode('utf-8', 'replace'))}"
    except (urllib.error.URLError, OSError) as error:
        return f"not answering ({getattr(error, 'reason', error)})"


def compact(body: str) -> str:
    try:
        return json.dumps(json.loads(body), separators=(",", ":"))[:1500]
    except ValueError:
        return " ".join(body.split())[:600]


def endpoints() -> dict[str, str]:
    """The dev stack's, and the control plane the `neura` command is signed in to."""
    found = dict(ENDPOINTS)
    try:
        from neura_cli.config import Config

        api = Config.load().api_url.rstrip("/")
    except Exception:  # no CLI here, or no config: the dev stack's are still worth asking
        return found
    if api != "http://127.0.0.1:8080":
        found[f"control plane the CLI uses ({api}), ready"] = f"{api}/readyz"
    return found


def run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
    return result.stdout + result.stderr


def compose(report: Report, lines: int) -> None:
    if not shutil.which("docker"):
        report.add("Dev stack (make dev)", "docker is not installed")
        return
    raw = run([*COMPOSE, "ps", "--all", "--format", "json"]).strip()
    services = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        services.extend(item if isinstance(item, list) else [item])
    if not services:
        report.add("Dev stack (make dev)", "not running (`make dev` starts it)")
        return
    rows = ["| Service | State | Health |", "| --- | --- | --- |"]
    for s in services:
        rows.append(f"| {s.get('Service')} | {s.get('State')} | {s.get('Health') or '-'} |")
    report.add("Dev stack (make dev)", "\n".join(rows))
    for s in services:
        name = str(s.get("Service"))
        log = run([*COMPOSE, "logs", "--no-color", "--no-log-prefix", "--tail", str(lines), name])
        report.scan(f"dev stack, {name}", log)
        report.add(f"Log: {name} (last {lines} lines)", f"```\n{log.strip()[-12000:]}\n```")


def local_sites(report: Report, lines: int) -> None:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    sites = sorted(p for p in (base / "neura" / "sites").glob("*") if (p / "agent.env").exists())
    if not sites:
        report.add("Sites on this machine (neura site join)", "none")
        return
    for site in sites:
        env = dict(
            line.partition("=")[::2]
            for line in (site / "agent.env").read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        pid_file = site / "agent.pid"
        pid = pid_file.read_text(encoding="utf-8").strip() if pid_file.exists() else ""
        alive = bool(pid) and Path(f"/proc/{pid}").exists()
        port = env.get("NEURA_AGENT_LOCAL_PORT", "8090")
        body = [
            f"- directory: `{site}`",
            f"- process: {pid or 'no pid file'} ({'running' if alive else 'not running'})",
            f"- local API: {probe(f'http://127.0.0.1:{port}/v1/local/status')}",
        ]
        log_file = site / "agent.log"
        if log_file.exists():
            text = "\n".join(
                log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
            )
            report.scan(f"site {site.name}, agent.log", text)
            body.append(f"\n```\n{text.strip()[-12000:]}\n```")
        report.add(f"Site {site.name} (neura site join)", "\n".join(body))


def postgres() -> str:
    try:
        with socket.create_connection(("127.0.0.1", 5432), timeout=1):
            return "answering on 127.0.0.1:5432"
    except OSError:
        return "not answering on 127.0.0.1:5432 (`service postgresql start`, or `make dev`)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--lines", type=int, default=120, help="lines kept of each log")
    args = parser.parse_args()
    report = Report()
    report.add("Postgres", postgres())
    report.add(
        "Endpoints", "\n".join(f"- {name}: {probe(url)}" for name, url in endpoints().items())
    )
    compose(report, args.lines)
    local_sites(report, args.lines)
    head = "# Dev report\n\n## What went wrong first\n\n"
    head += "\n\n".join(report.failures[:30]) if report.failures else "Nothing in the logs."
    text = head + "\n\n" + "\n".join(report.sections)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n(written to {OUT.relative_to(ROOT)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
