"""Robots a server finds, reports and runs, and bench sites approving their own activations
(design §29.4, §29.5)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_core import REV1, ROBOTS, Site

from neura.v1 import site_pb2
from neura_agent import robots
from neura_agent.core import BENCH_APPROVER
from neura_agent.gateway import build_gateway_config


def write_robots_file(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text(yaml.safe_dump({"robots": entries}), encoding="utf-8")
    return path


def test_simulated_robots_are_found_ready(tmp_path: Path) -> None:
    found = robots.discover(simulated=2, robots_file=None, scan_serial=False)
    assert [r.local_name for r in found] == ["sim-1", "sim-2"]
    assert all(r.simulated and r.state == "ready" and r.driver == "simulated" for r in found)
    assert found[0].profile["kind"] == "RobotProfile"
    inventory = robots.inventory(found)
    assert json.loads(inventory.robots[0].robot_profile_json)["metadata"]["name"] == (
        "so101-follower"
    )


def test_a_robots_file_configures_arms_and_customer_drivers(tmp_path: Path) -> None:
    profile = tmp_path / "picker.robot.yaml"
    profile.write_text(
        (robots.SO101_PROFILE).read_text(encoding="utf-8").replace("so101-follower", "picker"),
        encoding="utf-8",
    )
    path = write_robots_file(
        tmp_path / "robots.yaml",
        [
            {"name": "arm-1", "driver": "so101", "port": "/dev/ttyACM0", "cameras": {}},
            {"name": "picker-7", "driver": "acme", "profile": str(profile), "command": ["true"]},
            {"name": "picker-8", "driver": "acme", "profile": str(profile)},
        ],
    )
    found = {
        r.local_name: r for r in robots.discover(simulated=0, robots_file=path, scan_serial=False)
    }
    assert found["arm-1"].driver == "so101"
    assert found["picker-7"].profile["metadata"]["name"] == "picker"
    assert found["picker-7"].state == "ready"
    assert found["picker-8"].state == "needs_setup"
    assert "give the command" in found["picker-8"].message


def test_a_bad_robots_file_is_an_error_not_silence(tmp_path: Path) -> None:
    duplicate = write_robots_file(
        tmp_path / "robots.yaml",
        [{"name": "sim-1", "driver": "simulated"}],
    )
    with pytest.raises(robots.RobotsFileError, match="unique"):
        robots.discover(simulated=1, robots_file=duplicate, scan_serial=False)
    missing = write_robots_file(tmp_path / "other.yaml", [{"name": "x", "driver": "acme"}])
    with pytest.raises(robots.RobotsFileError, match="needs a `profile`"):
        robots.discover(simulated=0, robots_file=missing, scan_serial=False)


def test_plugged_in_so101_adapters_are_reported_as_needing_setup(tmp_path: Path) -> None:
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-1a86_USB_Single_Serial_5A46-if00").touch()
    (by_id / "usb-FTDI_other-if00").touch()
    found = robots.discover(simulated=0, robots_file=None, scan_serial=True, serial_directory=by_id)
    assert [(r.local_name, r.state) for r in found] == [("so101-1", "needs_setup")]
    assert "robots file" in found[0].message


def test_launch_commands(tmp_path: Path) -> None:
    sim = robots.discover(simulated=1, robots_file=None, scan_serial=False)[0]
    with_tls = robots.launch_for(
        "sim-1",
        sim,
        gateway="127.0.0.1:50051",
        credentials_dir=tmp_path,
        token=None,
        config_dir=tmp_path,
    )
    assert with_tls.command[1:4] == ("-m", "neura_sim.grpc_robots", "--assigned")
    assert "--cert-dir" in with_tls.command
    with_token = robots.launch_for(
        "sim-1",
        sim,
        gateway="127.0.0.1:50051",
        credentials_dir=tmp_path,
        token="t0k",
        config_dir=tmp_path,
    )
    assert with_token.command[-2:] == ("--token-template", "t0k")
    arm = robots.FoundRobot(
        local_name="arm-1",
        driver="so101",
        profile=sim.profile,
        port="/dev/ttyACM0",
        config={"cameras": {"front": {"index_or_path": 0}}, "calibration_id": "arm-1"},
    )
    launch = robots.launch_for(
        "plant-arm-1",
        arm,
        gateway="127.0.0.1:50051",
        credentials_dir=tmp_path,
        token=None,
        config_dir=tmp_path / "config",
    )
    config = yaml.safe_load(Path(launch.command[-1]).read_text(encoding="utf-8"))
    assert config["robot_id"] == "plant-arm-1"
    assert config["arm"] == {"type": "so101", "port": "/dev/ttyACM0", "calibration_id": "arm-1"}
    assert config["credentials"]["cert_dir"] == str(tmp_path)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_supervisor_restarts_and_stops_robots(tmp_path: Path) -> None:
    sim = robots.discover(simulated=1, robots_file=None, scan_serial=False)[0]
    sleeper = robots.Launch("a", sim, (sys.executable, "-c", "import time; time.sleep(30)"), {})
    crasher = robots.Launch("b", sim, (sys.executable, "-c", "raise SystemExit(3)"), {})
    clock = Clock()

    async def run() -> None:
        supervisor = robots.RobotSupervisor(tmp_path / "logs", clock=clock)

        async def noticed_exit_at(now: float) -> None:
            await asyncio.sleep(0.5)  # b exits at once
            clock.now = now
            await supervisor.tick()

        await supervisor.sync([sleeper, crasher])
        await noticed_exit_at(0.0)  # restarted in 1 s
        clock.now = 1.0
        await supervisor.tick()
        await noticed_exit_at(1.0)  # in 2 s
        clock.now = 3.0
        await supervisor.tick()
        # This run was healthy (97 s): the next wait is 1 s again, not the next in a crash loop.
        await noticed_exit_at(100.0)
        clock.now = 101.0
        await supervisor.tick()
        assert "b" in supervisor.running
        assert "a" in supervisor.running
        assert supervisor.status()["b"]["restarts"] == 3
        await supervisor.sync([crasher])
        assert "a" not in supervisor.running
        await supervisor.stop()

    asyncio.run(run())


@pytest.fixture
def bench(tmp_path: Path) -> Site:
    return Site(tmp_path, bench=True, simulated_robots=1)


async def test_a_found_robot_is_reported_once_and_run_once_in_the_manifest(bench: Site) -> None:
    core = bench.core
    assert core.refresh_robots()
    assert not core.refresh_robots()  # nothing changed: nothing sent again
    sent = [m for m in bench.drain() if m.WhichOneof("body") == "inventory"]
    assert [r.local_name for r in sent[0].inventory.robots] == ["sim-1"]
    manifest = bench.manifest([REV1])
    manifest["robots"].append(
        {"id": "rbt_01J000000000000000000000S1", "name": "bench-sim-1", "local_name": "sim-1"}
    )
    try:
        message = await core.on_desired_state(bench.desired(manifest))
        assert message.ack.accepted
        assert [launch.name for launch in core._launches] == ["bench-sim-1"]
        assert core.robots.running == ["bench-sim-1"]
        credentials = bench.settings.state_dir / "robots" / "bench-sim-1"
        assert (credentials / "bench-sim-1.pem").exists()
        assert (credentials / "bench-sim-1.key").stat().st_mode & 0o077 == 0
        config = core.gateway_config()
        assert "bench-sim-1" in {d["robot_id"] for d in config["devices"]}
    finally:
        await core.robots.stop()


def test_a_bench_approves_its_own_activations_only_when_both_sides_say_bench(
    tmp_path: Path,
) -> None:
    async def activate(site: Site, kind: str) -> list[site_pb2.OperationReport]:
        await site.core.on_desired_state(
            site.desired(site.manifest([REV1], site={"name": "lab", "kind": kind}))
        )
        await site.core.on_command(site.prepare(REV1["id"]))
        site.drain()
        await site.core.on_command(site.activate(REV1["id"]))
        return site.reports()

    approved = asyncio.run(activate(Site(tmp_path / "a", bench=True), "bench"))
    assert [r.state for r in approved] == ["Running", "Running"]
    assert approved[-1].message.startswith(f"approved by {BENCH_APPROVER}")
    assert json.loads(approved[-1].result_json) == {"approved_by": BENCH_APPROVER}

    # The cloud says bench, but the server was not installed as one: a person approves.
    waiting = asyncio.run(activate(Site(tmp_path / "b"), "bench"))
    assert [r.message for r in waiting] == ["awaiting local approval at the site"]
    # Installed as a bench, but the cloud made it a production site: a person approves.
    production = asyncio.run(activate(Site(tmp_path / "c", bench=True), "production"))
    assert [r.message for r in production] == ["awaiting local approval at the site"]


async def test_gateway_config_admits_managed_robots_with_tokens_without_tls(
    tmp_path: Path,
) -> None:
    site = Site(tmp_path, simulated_robots=1, gateway_tls=False)
    site.core.refresh_robots()
    manifest = site.manifest([REV1])
    manifest["robots"].append(
        {"id": "rbt_01J000000000000000000000S1", "name": "sim-1", "local_name": "sim-1"}
    )
    try:
        await site.core.on_desired_state(site.desired(manifest))
        config = build_gateway_config(site.core.state, site.settings, site.core.gateway_dir)
        assert "sim-1" in {d["robot_id"] for d in config["devices"]}
        assert site.core._launches[0].command[-2] == "--token-template"
        assert set(ROBOTS.values()) <= {r["name"] for r in manifest["robots"]}
    finally:
        await site.core.robots.stop()
