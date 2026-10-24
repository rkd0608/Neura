"""The `neura` command against a real control plane, and a real site agent it starts on this
machine (design §29): sign in through the browser, then the quickstart from a fresh workspace
to a simulated arm running a pushed policy, a new version, and a rollback."""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2 as httpx
import psycopg
import pytest

from conftest import LiveApiFactory, OidcIssuer
from neura_cli.app import main
from neura_cli.config import Config
from neura_cli.local_site import LocalSite
from neura_control.security.principal import Role

ADA = "https://sso.example.com|ada"

POLICY = '''
import numpy as np

import neura


@neura.policy(chunk_length=10, cameras={}, weights="weights")
class Hold:
    """Holds the arm a little off where it is."""

    def __init__(self, weights, device):
        self.offset = float((weights / "offset.txt").read_text())

    def act(self, observation):
        return np.tile(observation["observation.state"] + self.offset, (10, 1))
'''


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("NEURA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for name in ("NEURA_TOKEN", "NEURA_API_URL", "NEURA_WORKSPACE", "NEURA_PUBLISHER_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield tmp_path
    for name, directory in Config.load().local_sites.items():
        LocalSite.at(name, directory).stop()


def neura(capsys: pytest.CaptureFixture[str], *argv: str, code: int = 0) -> str:
    result = main(list(argv))
    captured = capsys.readouterr()
    assert result == code, f"neura {' '.join(argv)} -> {result}\n{captured.out}\n{captured.err}"
    return captured.out


def neura_json(capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    return json.loads(neura(capsys, "--json", *argv))


def wait_for(condition: Callable[[], Any], timeout: float = 60, what: str = "it") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := condition():
            return value
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def test_neura_login_signs_in_through_the_browser(
    make_live_api: LiveApiFactory,
    home: Path,
    capsys: pytest.CaptureFixture[str],
    database_url: str,
) -> None:
    issuer = OidcIssuer()
    api = make_live_api(oidc_verifier=issuer.verifier, memberships={ADA: Role.ADMIN})
    signed_in: list[int] = []
    cli = threading.Thread(
        target=lambda: signed_in.append(main(["login", "--api", api.url, "--no-browser"]))
    )
    cli.start()

    def pending_code() -> str | None:
        with psycopg.connect(database_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT user_code FROM login_sessions WHERE state = 'pending'"
            ).fetchone()
        return None if row is None else str(row[0])

    code = wait_for(pending_code, timeout=20, what="the CLI to start a sign-in")
    # The person, signed in to the console with their SSO, approves the code the CLI shows.
    approved = httpx.post(
        f"{api.url}/v1/login-approvals",
        json={"user_code": code},
        headers={"Authorization": f"Bearer {issuer.token('ada')}", "Idempotency-Key": "k" * 16},
    )
    assert approved.status_code == 200, approved.text
    cli.join(timeout=30)
    assert signed_in == [0]
    printed = capsys.readouterr().out
    assert code in printed
    assert "Signed in to Acme Robotics" in printed
    config = Config.load()
    assert config.token is not None
    assert config.token.startswith("ntk_")
    assert config.organization_id == api.organization_id
    assert (home / "config" / "config.json").stat().st_mode & 0o077 == 0
    whoami = neura_json(capsys, "whoami")
    assert whoami["workspace"]["organization_id"] == api.organization_id
    assert whoami["principal"]["kind"] == "api_token"
    neura(capsys, "logout")
    assert Config.load().token is None


def test_the_quickstart_from_a_fresh_workspace_to_a_moving_arm(
    make_live_api: LiveApiFactory, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        endpoint = f"127.0.0.1:{s.getsockname()[1]}"
    api = make_live_api(
        settings_overrides={
            "agent_grpc_listen": endpoint,
            "agent_endpoint": endpoint,
            "agent_poll_interval_s": 0.2,
            "rollout_sweep_interval_s": 0.5,
            "manifest_signing_key": "x" * 43,
        }
    )
    # Sign in with a token, as CI does. The bench runs real policy workers (as processes).
    stdin = io.StringIO(api.token + "\n")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("sys.stdin", stdin)
        assert "Signed in to Acme Robotics" in neura(
            capsys, "login", "--api", api.url, "--with-token"
        )

    # A bench, and this machine as its server with one simulated arm.
    assert "Created bench site lab" in neura(capsys, "site", "create", "lab", "--bench")
    joined = neura(capsys, "site", "join", "lab", "--simulate", "1")
    assert "Enrolled this machine" in joined
    assert "Agent running" in joined
    robots = wait_for(lambda: neura_json(capsys, "robots"), what="the arm to be found")
    assert [r["spec"]["name"] for r in robots] == ["sim-1"]
    assert robots[0]["status"]["discovered"]

    # A policy: a Python class with its weights. It is checked, signed and its files put where
    # the bench finds them; Neura never sees them.
    project = home / "hold"
    (project / "weights").mkdir(parents=True)
    (project / "hold.py").write_text(POLICY, encoding="utf-8")
    (project / "weights" / "offset.txt").write_text("0.0", encoding="utf-8")
    pushed = neura(capsys, "policy", "push", str(project / "hold.py"), "--name", "hold")
    assert "Fits so101-follower" in pushed
    assert "Signed" in pushed
    assert "Files at lab" in pushed
    assert "hold v1" in pushed
    # Pushing the same files again is the same version.
    assert "already had these files" in neura(
        capsys, "policy", "push", str(project / "hold.py"), "--name", "hold"
    )

    # Try it on the bench: no shadow, and the bench approves its own activation.
    tried = neura(capsys, "try", "hold", "--on", "sim-1", "--timeout", "120")
    assert "Live" in tried
    assert "approved by the bench site's agent" in tried
    # A real policy worker ran the class at the site, and the site timed it.
    assert "per pass (measured)" in tried

    def running(version: int) -> Any:
        robot = neura_json(capsys, "robots")[0]
        return robot if robot["status"].get("policy_version") == version else None

    robot = wait_for(lambda: running(1), what="the arm to run hold v1")
    assert robot["status"]["policy_name"] == "hold"

    # A new version, deployed; then a rollback to the one before.
    (project / "weights" / "offset.txt").write_text("0.1", encoding="utf-8")
    assert "hold v2" in neura(capsys, "policy", "push", str(project / "hold.py"), "--name", "hold")
    assert "Live" in neura(capsys, "deploy", "hold", "--to", "sim-1", "--timeout", "120")
    wait_for(lambda: running(2), what="the arm to run hold v2")
    assert "Live" in neura(capsys, "rollback", "sim-1", "--timeout", "120")
    wait_for(lambda: running(1), what="the arm to run hold v1 again")

    status = neura_json(capsys, "status")
    assert [r["status"]["state"] for r in status["rollouts"]] == ["live", "live", "live"]
    assert [r["spec"]["kind"] for r in status["rollouts"]] == ["rollback", "deploy", "try"]
    shown = neura(capsys, "policy", "show", "hold")
    assert "v2" in shown
    assert "v1" in shown
    logs = neura(capsys, "logs", "-n", "100")
    assert "lab found a simulated so101-follower: sim-1" in logs
    assert "doctor" not in logs
    doctor = neura(capsys, "doctor")
    assert "Site lab" in doctor
    assert "agent connected" in doctor
    assert "Stopped lab's agent" in neura(capsys, "site", "stop", "lab")
