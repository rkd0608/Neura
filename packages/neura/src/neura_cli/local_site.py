"""A site agent run by `neura site join` on this machine, for this user (design §29.5).

The quickest way to a working site: a laptop or a lab computer becomes a bench, finds the arms
plugged into it (or simulates some), and runs what is deployed to them. Everything the agent
keeps is under ``~/.local/share/neura/sites/<site>/``:

    agent.env        its settings (NEURA_AGENT_*), one per line
    agent/           its state: identity, site CA, artifact store, ledger
    weights/         files `neura policy push` puts there for it (sha256/<hex>)
    operator-token   the local operator credential `neura approve` uses (owner only)
    agent.log        what it says

It runs as a development agent: policy workers are plain processes, not containers, so it is for
benches and trying things, never for robots near people. A production site runs the agent as a
system service, installed by the command the console shows.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from neura_cli.session import CliError


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "neura" / "sites"


def agent_installed() -> bool:
    return importlib.util.find_spec("neura_agent") is not None


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return int(s.getsockname()[1])
    raise CliError("No free port on this machine.")


@dataclass
class LocalSite:
    name: str
    directory: Path

    @classmethod
    def at(cls, name: str, directory: str | Path | None = None) -> LocalSite:
        return cls(name, Path(directory) if directory else data_dir() / name)

    @property
    def state_dir(self) -> Path:
        return self.directory / "agent"

    @property
    def weights_dir(self) -> Path:
        return self.directory / "weights"

    @property
    def env_file(self) -> Path:
        return self.directory / "agent.env"

    @property
    def log_file(self) -> Path:
        return self.directory / "agent.log"

    @property
    def pid_file(self) -> Path:
        return self.directory / "agent.pid"

    @property
    def token_file(self) -> Path:
        return self.directory / "operator-token"

    def exists(self) -> bool:
        return self.env_file.is_file()

    # --- settings ---------------------------------------------------------------------------

    def environment(self) -> dict[str, str]:
        values = {}
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.startswith("NEURA_AGENT_"):
                values[key] = value
        return values

    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.environment().get('NEURA_AGENT_LOCAL_PORT', '8090')}"

    def operator_token(self) -> str:
        return self.token_file.read_text(encoding="utf-8").strip()

    def configure(
        self,
        *,
        api_url: str,
        bench: bool,
        simulated_robots: int,
        trusted_keys: list[str],
        mock_policies: bool,
        scan_serial: bool = True,
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.weights_dir.mkdir(exist_ok=True)
        token = "ntop_" + secrets.token_urlsafe(32)
        descriptor = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(token + "\n")
        gateway = _free_port(50051)
        settings: dict[str, Any] = {
            "STATE_DIR": self.state_dir,
            # A user's own machine: workers run as processes (development only, refused
            # otherwise), and nothing listens beyond loopback.
            "DEVELOPMENT": "true",
            "WORKER_SANDBOX": "process",
            "CONTROL_PLANE_URL": api_url,
            "CONTROL_PLANE_INSECURE": "true" if api_url.startswith("http://") else "false",
            "LOCAL_HOST": "127.0.0.1",
            "LOCAL_PORT": _free_port(8090),
            "GATEWAY_LISTEN": f"127.0.0.1:{gateway}",
            "GATEWAY_STATUS_LISTEN": f"127.0.0.1:{_free_port(8091)}",
            "CAPTURE_LISTEN": f"127.0.0.1:{(capture := _free_port(50052))}",
            "CAPTURE_ENDPOINT": f":{capture}",
            "BENCH": "true" if bench else "false",
            "SIMULATED_ROBOTS": simulated_robots,
            "SCAN_SERIAL_PORTS": "true" if scan_serial else "false",
            "ARTIFACT_SOURCES": json.dumps([str(self.weights_dir)]),
            "TRUSTED_PUBLISHER_KEYS": json.dumps(trusted_keys),
            "OPERATOR_TOKEN_SHA256": hashlib.sha256(token.encode()).hexdigest(),
            "PROBE_INTERVAL_S": "2",
        }
        if mock_policies:
            settings["GATEWAY_BACKEND"] = "simulated"
        robots_file = self.directory / "robots.yaml"
        if robots_file.is_file():
            settings["ROBOTS_FILE"] = robots_file
        lines = [f"NEURA_AGENT_{key}={value}" for key, value in settings.items()]
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.env_file.chmod(0o600)

    def _env(self) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if not k.startswith("NEURA_AGENT_")}
        return {**base, **self.environment()}

    # --- running ----------------------------------------------------------------------------

    def enroll(self, endpoint: str, token: str) -> str:
        # Our own interpreter and the agent's module; the endpoint and token are its arguments.
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "neura_agent",
                "enroll",
                "--control-plane",
                endpoint,
                "--token",
                token,
            ],
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise CliError(
                "This machine could not join the site.",
                *(result.stderr.strip().splitlines()[-3:] or ["the agent said nothing"]),
            )
        return result.stdout.strip()

    def pid(self) -> int | None:
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        with contextlib.suppress(ChildProcessError):  # started by this process: reap it
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return pid
        return pid

    def start(self) -> int:
        """Start the agent in its own session, detached from this terminal, writing to its
        log. It keeps running after this command ends."""
        running = self.pid()
        if running is not None:
            return running
        log = os.open(self.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        null = os.open(os.devnull, os.O_RDONLY)
        try:
            pid = os.posix_spawn(
                sys.executable,
                # -P: nothing from the current directory shadows the agent's modules.
                [sys.executable, "-P", "-m", "neura_agent", "run"],
                self._env(),
                file_actions=[
                    (os.POSIX_SPAWN_DUP2, null, 0),
                    (os.POSIX_SPAWN_DUP2, log, 1),
                    (os.POSIX_SPAWN_DUP2, log, 2),
                ],
                setsid=True,
            )
        finally:
            os.close(log)
            os.close(null)
        self.pid_file.write_text(f"{pid}\n", encoding="utf-8")
        return pid

    def run_in_foreground(self) -> int:
        """Run the agent attached to this terminal until it stops (Ctrl-C)."""
        with contextlib.suppress(KeyboardInterrupt):
            return subprocess.call(
                [sys.executable, "-m", "neura_agent", "run"], env=self._env(), cwd=self.directory
            )
        return 0

    def stop(self, timeout_s: float = 20.0) -> bool:
        pid = self.pid()
        if pid is None:
            return False
        os.kill(pid, signal.SIGTERM)  # the agent stops its robots and gateway first
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and self.pid() is not None:
            time.sleep(0.2)
        # Whatever is left of its session (the agent's own children included) goes too.
        for sent in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, sent)
            time.sleep(0.5 if sent == signal.SIGTERM else 0)
        self.pid()  # reap it
        self.pid_file.unlink(missing_ok=True)
        return True

    def status(self) -> dict[str, Any] | None:
        try:
            response = httpx.get(f"{self.local_url()}/v1/local/status", timeout=2)
        except httpx.HTTPError:
            return None
        return response.json() if response.status_code == 200 else None

    def wait_until_connected(self, timeout_s: float = 60.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        status: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status = self.status()
            if status and status.get("connection", {}).get("connected"):
                return status
            if self.pid() is None:
                break
            time.sleep(0.5)
        tail = self.log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        raise CliError(
            "The site agent did not connect to Neura.",
            *(tail or ["it wrote nothing"]),
            f"Its log: {self.log_file}",
        )

    # --- the local API ----------------------------------------------------------------------

    def call(self, method: str, path: str, body: Any = None) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self.local_url()}{path}",
                json=body,
                headers={"Authorization": f"Bearer {self.operator_token()}"},
                timeout=10,
            )
        except httpx.HTTPError as error:
            raise CliError(
                f"The agent of {self.name} is not answering.",
                f"Start it: neura site start {self.name}",
            ) from error
        if response.status_code >= 400:
            problem = response.json() if response.content else {}
            detail = problem.get("detail") or response.status_code
            raise CliError(f"The agent of {self.name} refused: {detail}.")
        return response.json()
