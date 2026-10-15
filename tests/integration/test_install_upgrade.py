"""Installing the site agent and upgrading it (design §17.2, §17.3), with real bundles: built and
signed by tools/build_agent_release.py, verified with openssl by deploy/install/install.sh,
installed into virtual environments with pip, switched, committed and rolled back.

The releases hold a stand-in `neura-agent` wheel that reports its version, so the test runs in
seconds and offline.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from neura.v1 import site_pb2
from neura_agent import launch
from neura_agent.releases import Installation, UpgradeState
from neura_agent.settings import AgentSettings
from neura_agent.state import AuditLog
from neura_agent.upgrade import Upgrader

ROOT = Path(__file__).resolve().parents[2]
BUILD = [sys.executable, str(ROOT / "tools" / "build_agent_release.py")]
INSTALL = ROOT / "deploy" / "install" / "install.sh"
PYTHON = getattr(sys, "_base_executable", None) or sys.executable


def stub_wheel(directory: Path, version: str) -> Path:
    """A wheel named neura-agent whose `neura-agent` prints its version and arguments."""
    dist = f"neura_agent-{version}.dist-info"
    files = {
        "neura_agent_stub/__init__.py": (
            "import json, os, sys\n"
            "def main():\n"
            f"    print(json.dumps({{'version': '{version}', 'argv': sys.argv[1:]}}))\n"
            "    sys.exit(int(os.environ.get('NEURA_STUB_EXIT', '0')))\n"
        ),
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: neura-agent\nVersion: {version}\n",
        f"{dist}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: neura-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist}/entry_points.txt": "[console_scripts]\nneura-agent = neura_agent_stub:main\n",
    }
    record = []
    for name, text in files.items():
        data = text.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{name},sha256={digest},{len(data)}")
    record.append(f"{dist}/RECORD,,")
    files[f"{dist}/RECORD"] = "\n".join(record) + "\n"
    directory.mkdir(parents=True, exist_ok=True)
    wheel = directory / f"neura_agent-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return wheel


def release(tmp_path: Path, version: str, key: Path) -> tuple[Path, str]:
    wheels = tmp_path / f"wheels-{version}"
    stub_wheel(wheels, version)
    out = tmp_path / "dist"
    done = subprocess.run(
        [
            *BUILD,
            "build",
            "--key",
            str(key),
            "--out",
            str(out),
            "--version",
            version,
            "--wheels-from",
            str(wheels),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    digest = done.stdout.split("sha256 ")[1].strip()
    return out / f"neura-agent-{version}.tar", digest


@pytest.fixture
def keys(tmp_path: Path) -> tuple[Path, Path]:
    subprocess.run([*BUILD, "keygen", "--out", str(tmp_path / "release")], check=True)
    return tmp_path / "release.pem", tmp_path / "release.pub"


def install(tmp_path: Path, bundle: Path, public: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sh",
            str(INSTALL),
            "--prefix",
            str(tmp_path / "root"),
            "--no-systemd",
            "--python",
            PYTHON,
            "--bundle",
            str(bundle),
            "--release-keys",
            str(public),
            "--control-plane",
            "cp.example:8443",
            "--token",
            "nen_example",
            "--env",
            "NEURA_AGENT_WORKER_SANDBOX=container",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_installer_verifies_installs_and_enrolls_a_signed_release(
    tmp_path: Path, keys: tuple[Path, Path]
) -> None:
    private, public = keys
    bundle, _ = release(tmp_path, "9.9.1", private)
    done = install(tmp_path, bundle, public)
    assert done.returncode == 0, done.stderr
    assert "verified neura-agent 9.9.1" in done.stdout
    enrolled = json.loads(next(line for line in done.stdout.splitlines() if line.startswith("{")))
    assert enrolled == {
        "version": "9.9.1",
        "argv": ["enroll", "--control-plane", "cp.example:8443", "--token", "nen_example"],
    }
    root = tmp_path / "root"
    agent = root / "opt" / "neura" / "agent"
    assert Installation(agent).current_version() == "9.9.1"
    assert (root / "etc" / "neura" / "release-keys.pem").read_bytes() == public.read_bytes()
    env = (root / "etc" / "neura" / "agent.env").read_text()
    assert "NEURA_AGENT_STATE_DIR=/var/lib/neura-agent" in env
    assert "NEURA_AGENT_WORKER_SANDBOX=container" in env
    unit = (root / "etc" / "systemd" / "system" / "neura-agent.service").read_text()
    for line in (
        "ExecStart=" + PYTHON + " /opt/neura/agent/bin/neura-agent-launch run",
        "Restart=always",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ReadWritePaths=/var/lib/neura-agent /opt/neura/agent",
    ):
        assert line in unit
    # systemd's command: the launcher runs the current release.
    launched = subprocess.run(
        [PYTHON, str(agent / "bin" / "neura-agent-launch"), "status"],
        env={**os.environ, "NEURA_AGENT_INSTALL_DIR": str(agent), "NEURA_AGENT_STATE_DIR": "/x"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(launched.stdout) == {"version": "9.9.1", "argv": ["status"]}


def test_the_installer_refuses_bundles_not_signed_by_the_release_keys(
    tmp_path: Path, keys: tuple[Path, Path]
) -> None:
    private, public = keys
    subprocess.run([*BUILD, "keygen", "--out", str(tmp_path / "other")], check=True)
    foreign, _ = release(tmp_path / "foreign", "9.9.1", tmp_path / "other.pem")
    done = install(tmp_path, foreign, public)
    assert done.returncode != 0
    assert "not signed by a release key" in done.stderr
    # A signed bundle with a file changed after signing.
    bundle, _ = release(tmp_path, "9.9.1", private)
    tampered = tmp_path / "tampered"
    tampered.mkdir()
    subprocess.run(["tar", "-xf", str(bundle), "-C", str(tampered)], check=True)
    wheel = next(tampered.rglob("*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"\0")
    repacked = tmp_path / "tampered.tar"
    subprocess.run(["tar", "-cf", str(repacked), "-C", str(tampered), "."], check=True)
    done = install(tmp_path, repacked, public)
    assert done.returncode != 0
    assert "do not match" in done.stderr
    assert not (tmp_path / "root" / "opt" / "neura" / "agent" / "current").exists()


# --- upgrades ---------------------------------------------------------------------------------


class FakeGateway:
    def __init__(self, busy_reads: int = 0) -> None:
        self.busy_reads = busy_reads

    async def status(self) -> dict[str, Any]:
        state = "Running" if self.busy_reads > 0 else "Ready"
        self.busy_reads -= 1
        return {"sessions": [{"robot_id": "arm-1", "robot_state": state}]}


class FakeCore:
    """What the upgrader uses of the agent core."""

    def __init__(self, settings: AgentSettings, gateway: FakeGateway) -> None:
        self.settings = settings
        self.gateway = gateway
        self.audit = AuditLog(settings.state_dir / "audit.jsonl")
        self.reports: list[dict[str, Any]] = []

    def report(self, command_id: str, operation_id: str, state: str, **kw: Any) -> None:
        reason = kw.get("reason")
        self.reports.append(
            {"command": command_id, "state": state, "reason": reason.value if reason else ""}
            | {k: v for k, v in kw.items() if k in ("message", "result")}
        )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def installed(tmp_path: Path, keys: tuple[Path, Path]) -> dict[str, Any]:
    private, public = keys
    first, _ = release(tmp_path, "9.9.1", private)
    assert install(tmp_path, first, public).returncode == 0
    second, digest = release(tmp_path, "9.9.2", private)
    root = tmp_path / "root"
    settings = AgentSettings(
        state_dir=root / "var" / "lib" / "neura-agent",
        install_dir=root / "opt" / "neura" / "agent",
        release_keys_file=root / "etc" / "neura" / "release-keys.pem",
        upgrade_health_timeout_s=60,
    )
    request = site_pb2.UpgradeAgent(
        server_id="srv_1",
        version="9.9.2",
        bundle_url=second.as_uri(),
        bundle_sha256=digest,
    )
    return {"settings": settings, "request": request, "private": private}


def upgrader(
    settings: AgentSettings,
    version: str,
    *,
    connected: bool = False,
    gateway: FakeGateway | None = None,
    clock: Clock | None = None,
) -> tuple[Upgrader, FakeCore, list[str]]:
    core = FakeCore(settings, gateway or FakeGateway())
    restarts: list[str] = []
    up = Upgrader(
        core,  # type: ignore[arg-type]
        version=version,
        connected=lambda: connected,
        restart=lambda: restarts.append(version),
        monotonic=clock or Clock(),
    )
    return up, core, restarts


async def test_an_upgrade_is_verified_staged_switched_at_a_task_boundary_and_committed(
    installed: dict[str, Any],
) -> None:
    settings = installed["settings"]
    old, core, restarts = upgrader(settings, "9.9.1", gateway=FakeGateway(busy_reads=2))
    await old.run("cmd_1", "opr_1", installed["request"])
    assert [r["state"] for r in core.reports] == ["Running"] * 4
    assert restarts == ["9.9.1"]  # the agent restarts into the new release
    assert Installation(settings.install_dir).current_version() == "9.9.2"
    state = UpgradeState.load(settings.state_dir)
    assert state is not None
    assert (state.state, state.from_version, state.to_version) == ("switching", "9.9.1", "9.9.2")
    # systemd starts the launcher, which counts the attempt and runs 9.9.2; it reconnects.
    assert launch.prepare(settings.install_dir, settings.state_dir) is not None
    new, core, _ = upgrader(settings, "9.9.2", connected=True)
    new.check()
    assert core.reports == [
        {
            "command": "cmd_1",
            "state": "Succeeded",
            "reason": "",
            "result": {"agent_version_from": "9.9.1", "agent_version_to": "9.9.2"},
        }
    ]
    assert UpgradeState.load(settings.state_dir) is None
    assert sorted(p.name for p in (settings.install_dir / "releases").iterdir()) == [
        "9.9.1",
        "9.9.2",
    ]


async def test_a_release_that_cannot_start_is_rolled_back_by_the_launcher(
    installed: dict[str, Any],
) -> None:
    settings = installed["settings"]
    old, _, _ = upgrader(settings, "9.9.1")
    await old.run("cmd_1", "opr_1", installed["request"])
    for _ in range(launch.MAX_ATTEMPTS):
        assert "starting 9.9.2" in (launch.prepare(settings.install_dir, settings.state_dir) or "")
    assert "rolled back to 9.9.1" in (
        launch.prepare(settings.install_dir, settings.state_dir) or ""
    )
    assert Installation(settings.install_dir).current_version() == "9.9.1"
    survivor, core, _ = upgrader(settings, "9.9.1", connected=True)
    survivor.check()
    [report] = core.reports
    assert (report["state"], report["reason"]) == ("Failed", "AGENT_UPGRADE_ROLLED_BACK")
    assert "did not start" in report["message"]
    assert UpgradeState.load(settings.state_dir) is None


async def test_a_release_that_does_not_reconnect_is_rolled_back_by_itself(
    installed: dict[str, Any],
) -> None:
    settings = installed["settings"]
    old, _, _ = upgrader(settings, "9.9.1")
    await old.run("cmd_1", "opr_1", installed["request"])
    clock = Clock()
    new, core, restarts = upgrader(settings, "9.9.2", clock=clock)
    new.check()
    assert restarts == []  # still within the health timeout
    clock.now = settings.upgrade_health_timeout_s + 1
    new.check()
    assert restarts == ["9.9.2"]
    assert Installation(settings.install_dir).current_version() == "9.9.1"
    survivor, core, _ = upgrader(settings, "9.9.1")
    survivor.check()
    assert [(r["state"], r["reason"]) for r in core.reports] == [
        ("Failed", "AGENT_UPGRADE_ROLLED_BACK")
    ]


async def test_upgrades_refuse_bundles_that_are_not_the_signed_release_named(
    installed: dict[str, Any], tmp_path: Path
) -> None:
    settings = installed["settings"]
    request = installed["request"]
    cases = {
        "wrong digest": site_pb2.UpgradeAgent(
            server_id="srv_1",
            version="9.9.2",
            bundle_url=request.bundle_url,
            bundle_sha256="0" * 64,
        ),
        "wrong version": site_pb2.UpgradeAgent(
            server_id="srv_1",
            version="9.9.3",
            bundle_url=request.bundle_url,
            bundle_sha256=request.bundle_sha256,
        ),
    }
    subprocess.run([*BUILD, "keygen", "--out", str(tmp_path / "other")], check=True)
    foreign, digest = release(tmp_path / "foreign", "9.9.2", tmp_path / "other.pem")
    cases["foreign key"] = site_pb2.UpgradeAgent(
        server_id="srv_1", version="9.9.2", bundle_url=foreign.as_uri(), bundle_sha256=digest
    )
    for name, case in cases.items():
        up, core, restarts = upgrader(settings, "9.9.1")
        await up.run("cmd_1", "opr_1", case)
        assert (core.reports[-1]["state"], core.reports[-1]["reason"]) == (
            "Failed",
            "AGENT_UPGRADE_REFUSED",
        ), name
        assert restarts == []
        assert Installation(settings.install_dir).current_version() == "9.9.1"
    not_installed = settings.model_copy(update={"install_dir": tmp_path / "nowhere"})
    up, core, _ = upgrader(not_installed, "9.9.1")
    await up.run("cmd_1", "opr_1", request)
    assert "installer" in core.reports[-1]["message"]


def test_remote_upgrades_are_off_until_the_site_operator_allows_them() -> None:
    assert "upgrade_agent" not in AgentSettings().allowed_operations
    allowed = AgentSettings(allowed_operations=["prepare", "upgrade_agent"])
    assert "upgrade_agent" in allowed.allowed_operations
