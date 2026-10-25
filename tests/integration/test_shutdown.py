"""Processes stopped the way they are stopped in practice (SIGTERM, from systemd, the site agent or
a test) clean up after themselves: the gateway's worker directory, the console tests' episode
site and its files, and their control plane's database.

Each process runs a uvicorn server. uvicorn handles SIGTERM itself and, once it has stopped,
raises the signal again, which kills the process before any cleanup around the server runs.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "examples" / "contracts"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def wait_for(condition: object, timeout_s: float = 60) -> None:
    assert callable(condition)
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.2)


def answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return bool(response.status == 200)
    except OSError:
        return False


def stop(process: subprocess.Popen[bytes]) -> int:
    process.send_signal(signal.SIGTERM)
    return process.wait(timeout=30)


def gateway_config(status: int | None = None) -> dict[str, object]:
    return {
        "listen": f"127.0.0.1:{free_port()}",
        "status_listen": f"127.0.0.1:{status}" if status else None,
        "devices": [{"robot_id": "robot-0", "token_sha256": "0" * 64}],
        "deployments": [
            {
                "revision_id": "rev_dev",
                "artifact": str(CONTRACTS / "act-so101.artifact.yaml"),
                "robot_profile": str(CONTRACTS / "so101.robot.yaml"),
                "execution_profile": str(CONTRACTS / "act-so101-30hz.profile.yaml"),
                "robots": ["robot-0"],
                "adapter": "mock",
                "device": "cpu",
            }
        ],
        "serving": {"backend": "workers"},
    }


def test_a_stopped_gateway_stops_its_workers_and_removes_their_directory(tmp_path: Path) -> None:
    status = free_port()
    config = gateway_config(status)
    (tmp_path / "gateway.yaml").write_text(json.dumps(config), encoding="utf-8")
    temp = tmp_path / "tmp"
    temp.mkdir()
    gateway = subprocess.Popen(
        [sys.executable, "-m", "neura_server", "--config", str(tmp_path / "gateway.yaml")],
        env={**os.environ, "TMPDIR": str(temp)},
    )
    try:

        def ready() -> bool:
            if not answers(f"http://127.0.0.1:{status}/v1/local/dataplane"):
                return False
            url = f"http://127.0.0.1:{status}/v1/local/dataplane"
            with urllib.request.urlopen(url, timeout=1) as response:
                groups = json.load(response)["groups"].values()
            return any(g["worker"]["state"] == "ready" for g in groups)

        wait_for(ready)
        (worker_dir,) = temp.glob("neura-w-*")
        assert worker_dir.is_dir()
    finally:
        code = stop(gateway)
    assert code == 0  # stopped, not killed
    assert list(temp.iterdir()) == []


def test_a_gateway_that_cannot_start_leaves_no_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from neura_server.config import GatewayConfig
    from neura_server.grpc_server import GatewayServer

    # Too long a path for the workers' Unix sockets: the gateway refuses to start.
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(deep))
    (tmp_path / "gateway.yaml").write_text(json.dumps(gateway_config()), encoding="utf-8")
    with pytest.raises(ValueError, match="too long for Unix sockets"):
        GatewayServer(GatewayConfig.load(tmp_path / "gateway.yaml"))
    assert list(deep.iterdir()) == []


def test_the_episode_site_removes_its_episodes_when_stopped(tmp_path: Path) -> None:
    port = free_port()
    site = subprocess.Popen(
        [sys.executable, str(ROOT / "console" / "e2e" / "episodes_site.py"), str(port)],
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    try:
        wait_for(lambda: answers(f"http://127.0.0.1:{port}/healthz"))
        assert any(tmp_path.iterdir())  # its episodes
    finally:
        code = stop(site)
    assert code == 0
    assert list(tmp_path.iterdir()) == []


def test_the_console_tests_control_plane_drops_its_database_when_stopped(tmp_path: Path) -> None:
    database = os.environ.get("NEURA_TEST_DATABASE_URL")
    if not database:
        pytest.skip("set NEURA_TEST_DATABASE_URL to run control-plane database tests")
    import psycopg

    state = tmp_path / f"stack-{uuid.uuid4().hex[:8]}.json"
    api, agent = free_port(), free_port()
    stack = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "console" / "e2e" / "stack.py"),
            "--state",
            str(state),
            "--api-port",
            str(api),
            "--agent-port",
            str(agent),
        ],
        env={**os.environ, "NEURA_E2E_DATABASE_URL": database},
    )
    try:
        wait_for(lambda: answers(f"http://127.0.0.1:{api}/readyz"))
        name = json.loads(state.read_text(encoding="utf-8"))["database"]
    finally:
        code = stop(stack)
    assert code == 0
    with psycopg.connect(database, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    assert row is None
