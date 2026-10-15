"""The stable launcher systemd runs (design §17.3): ``neura-agent-launch``.

It lives outside every release (the installer copies it to ``<install>/bin``) and needs only the
standard library, so a release that cannot even start cannot break it. Before it runs
``<install>/current``, it checks the upgrade in progress, if any: each start of a just-switched
release counts as an attempt, and after ``MAX_ATTEMPTS`` failed starts it points ``current`` back
at the previous release and records the rollback, which the old agent then reports.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

MAX_ATTEMPTS = 3


def _write(path: Path, data: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def _point_current_at(root: Path, version: str) -> None:
    temporary = root / f".current-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(Path("releases") / version)
    os.replace(temporary, root / "current")


def prepare(root: Path, state_dir: Path) -> str | None:
    """Count this start against an upgrade that is switching; roll back when it has failed too
    often. Returns what happened, for the log."""
    path = state_dir / "upgrade.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if state.get("state") != "switching":
        return None
    state["attempts"] = int(state.get("attempts", 0)) + 1
    if state["attempts"] > MAX_ATTEMPTS:
        _point_current_at(root, str(state["from_version"]))
        state["state"] = "rolled_back"
        state["reason"] = (
            f"neura-agent {state['to_version']} did not start in {MAX_ATTEMPTS} attempts"
        )
        state["rolled_back_at"] = datetime.datetime.now(datetime.UTC).isoformat()
        _write(path, state)
        return f"rolled back to {state['from_version']}: {state['reason']}"
    _write(path, state)
    return f"starting {state['to_version']} (attempt {state['attempts']} of {MAX_ATTEMPTS})"


def main(argv: list[str] | None = None) -> NoReturn:
    args = sys.argv[1:] if argv is None else argv
    root = Path(os.environ.get("NEURA_AGENT_INSTALL_DIR", "/opt/neura/agent"))
    state_dir = Path(os.environ.get("NEURA_AGENT_STATE_DIR", "/var/lib/neura-agent"))
    note = prepare(root, state_dir)
    if note:
        print(f"neura-agent-launch: {note}", file=sys.stderr, flush=True)
    executable = root / "current" / "venv" / "bin" / "neura-agent"
    os.execv(executable, [str(executable), *(args or ["run"])])  # noqa: S606 - our own binary


if __name__ == "__main__":
    main()
