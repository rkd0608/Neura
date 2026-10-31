"""The robots this server drives (design §29.5): found or configured, reported, and run.

- **Found.** Simulated robots (``NEURA_AGENT_SIMULATED_ROBOTS``), robots listed in the robots file
  (``NEURA_AGENT_ROBOTS_FILE``), and serial adapters of supported arms, which are reported as
  needing setup until the robots file configures them (an arm needs its cameras and calibration
  named, and the agent never guesses which camera is which).
- **Reported.** Each found robot goes to the control plane as a ``DiscoveredRobot``: its name at
  this server, its driver and its robot profile. The control plane registers the ones it does not
  know; nobody has to create robots or robot profiles by hand.
- **Run.** Once a found robot is in the site's signed manifest, the agent gives it credentials
  from the site CA (its key never leaves this server) and runs its runtime process: a simulated
  arm, ``neura-arm`` for an SO-101, or a customer's own driver command. A process that exits is
  restarted with backoff, as gateways and workers are (``neura_server.supervision``); one that is
  no longer wanted is stopped.

The robots file (YAML)::

    robots:
      - name: arm-1                 # unique at this server
        driver: so101
        port: /dev/serial/by-id/usb-1a86_USB_Single_Serial_5A46083062-if00
        calibration_id: arm-1
        cameras:
          front: {index_or_path: /dev/video0}
          wrist: {index_or_path: /dev/video2}
      - name: picker-7
        driver: acme-picker          # a customer driver: the command connects to the gateway
        profile: /etc/neura/picker.robot.yaml
        command: [/opt/acme/bin/picker-driver, --port, /dev/ttyUSB0]
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from neura.v1 import site_pb2
from neura_server.supervision import RestartBackoff

log = logging.getLogger("neura.agent.robots")

PROFILES = Path(__file__).parent / "profiles"
SO101_PROFILE = PROFILES / "so101-follower.robot.yaml"
# USB-to-serial bridges the SO-101's servo bus boards use (QinHeng CH340/CH343).
SO101_SERIAL_MARKERS = ("1a86",)
SERIAL_BY_ID = Path("/dev/serial/by-id")


class RobotsFileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FoundRobot:
    local_name: str
    driver: str
    profile: Mapping[str, Any]
    port: str = ""
    serial: str = ""
    simulated: bool = False
    state: str = "ready"  # ready | needs_setup
    message: str = ""
    # What the driver needs to run it (cameras, calibration, a command).
    config: Mapping[str, Any] = field(default_factory=dict)

    def message_pb(self) -> site_pb2.DiscoveredRobot:
        return site_pb2.DiscoveredRobot(
            local_name=self.local_name,
            driver=self.driver,
            robot_profile_json=json.dumps(dict(self.profile), sort_keys=True),
            port=self.port,
            serial=self.serial,
            simulated=self.simulated,
            state=self.state,
            message=self.message,
        )


def load_profile(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("kind") != "RobotProfile":
        raise RobotsFileError(f"{path} is not a RobotProfile document")
    return data


def _robots_file(path: Path) -> list[FoundRobot]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise RobotsFileError(f"cannot read {path}: {error}") from error
    entries = data.get("robots", []) if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise RobotsFileError(f"{path}: expected a `robots:` list")
    found: list[FoundRobot] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("driver"):
            raise RobotsFileError(f"{path}: every robot needs a name and a driver")
        driver = str(entry["driver"])
        profile_path = entry.get("profile")
        if profile_path:
            profile = load_profile(Path(str(profile_path)))
        elif driver in ("so101", "simulated"):
            profile = load_profile(SO101_PROFILE)
        else:
            raise RobotsFileError(f"{path}: {entry['name']} needs a `profile` for driver {driver}")
        state, message = "ready", ""
        if driver == "so101" and importlib.util.find_spec("neura_robots") is None:
            state, message = "needs_setup", "install neura-robots to drive an SO-101"
        if driver not in ("so101", "simulated") and not entry.get("command"):
            state, message = "needs_setup", f"give the command that runs the {driver} driver"
        found.append(
            FoundRobot(
                local_name=str(entry["name"]),
                driver=driver,
                profile=profile,
                port=str(entry.get("port", "")),
                serial=str(entry.get("serial", "")),
                simulated=driver == "simulated",
                state=state,
                message=message,
                config={k: v for k, v in entry.items() if k not in ("name", "driver", "profile")},
            )
        )
    return found


def _serial_arms(configured_ports: set[str], directory: Path = SERIAL_BY_ID) -> list[FoundRobot]:
    """SO-101 servo bus adapters plugged in but not in the robots file: reported so a person
    sees them, as needing setup."""
    if not directory.is_dir():
        return []
    found = []
    for index, path in enumerate(sorted(directory.iterdir()), start=1):
        if str(path) in configured_ports:
            continue
        if not any(marker in path.name.lower() for marker in SO101_SERIAL_MARKERS):
            continue
        found.append(
            FoundRobot(
                local_name=f"so101-{index}",
                driver="so101",
                profile=load_profile(SO101_PROFILE),
                port=str(path),
                state="needs_setup",
                message=(
                    "an SO-101 bus adapter is plugged in: add it to the robots file with its "
                    "cameras and calibration to run it"
                ),
            )
        )
    return found


def discover(
    *,
    simulated: int,
    robots_file: Path | None,
    scan_serial: bool,
    serial_directory: Path = SERIAL_BY_ID,
) -> list[FoundRobot]:
    """Every robot this server has, sorted by name. Raises ``RobotsFileError`` for a robots
    file that cannot be used, so the mistake is seen rather than its robots silently missing."""
    found: list[FoundRobot] = []
    if robots_file is not None:
        found.extend(_robots_file(robots_file))
    if simulated:
        profile = load_profile(SO101_PROFILE)
        found.extend(
            FoundRobot(
                local_name=f"sim-{i}",
                driver="simulated",
                profile=profile,
                simulated=True,
            )
            for i in range(1, simulated + 1)
        )
    if scan_serial:
        found.extend(_serial_arms({r.port for r in found if r.port}, serial_directory))
    names = [r.local_name for r in found]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise RobotsFileError(f"robot names must be unique at a server: {', '.join(duplicates)}")
    return sorted(found, key=lambda r: r.local_name)


def inventory(found: Sequence[FoundRobot]) -> site_pb2.RobotInventory:
    return site_pb2.RobotInventory(robots=[r.message_pb() for r in found])


# --- running robots ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Launch:
    """How to run one robot: its name in the manifest (and its certificate), and its process."""

    name: str
    robot: FoundRobot
    command: tuple[str, ...]
    env: Mapping[str, str]


def launch_for(
    name: str,
    robot: FoundRobot,
    *,
    gateway: str,
    credentials_dir: Path,
    token: str | None,
    config_dir: Path,
) -> Launch:
    """The process that runs ``robot`` as ``name`` against the site's gateway. With TLS the
    robot reads ``<name>.key``, ``<name>.pem`` and ``ca.pem`` from ``credentials_dir``; without
    it (development only) it sends ``token``."""
    env = {
        "NEURA_ROBOT_NAME": name,
        "NEURA_GATEWAY": gateway,
        "NEURA_ROBOT_CERT_DIR": str(credentials_dir),
    }
    if token is not None:
        env["NEURA_ROBOT_TOKEN"] = token
    if robot.driver == "simulated":
        command = [
            sys.executable,
            "-m",
            "neura_sim.grpc_robots",
            "--assigned",
            "--names",
            name,
            "--gateway",
            gateway,
            "--report-every",
            "60",
        ]
        if token is None:
            command += ["--cert-dir", str(credentials_dir), "--tls-server-name", "localhost"]
        else:
            command += ["--token-template", token]
        return Launch(name, robot, tuple(command), env)
    if robot.driver == "so101":
        config: dict[str, Any] = {
            "robot_id": name,
            "gateway": gateway,
            "credentials": (
                {"token": token}
                if token is not None
                else {"cert_dir": str(credentials_dir), "tls_server_name": "localhost"}
            ),
            "arm": {
                "type": "so101",
                "port": robot.port,
                **{
                    k: v
                    for k, v in robot.config.items()
                    if k in ("calibration_id", "calibration_dir", "max_relative_target")
                },
            },
            "cameras": dict(robot.config.get("cameras", {})),
        }
        if robot.config.get("capture_dir"):
            config["capture_dir"] = robot.config["capture_dir"]
        path = config_dir / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return Launch(
            name, robot, (sys.executable, "-m", "neura_robots.cli", "run", str(path)), env
        )
    driver_command = robot.config.get("command")
    if not isinstance(driver_command, list) or not driver_command:
        raise RobotsFileError(f"{robot.local_name}: driver {robot.driver} has no command")
    return Launch(name, robot, tuple(str(part) for part in driver_command), env)


class RobotSupervisor:
    """Runs the robots it is given and keeps them running; stops the ones taken away."""

    def __init__(self, log_dir: Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.log_dir = log_dir
        self._clock = clock
        self._wanted: dict[str, Launch] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._failures: dict[str, int] = {}  # restarts, as status shows them
        self._backoffs: dict[str, RestartBackoff] = {}
        self._restart_at: dict[str, float] = {}

    @property
    def running(self) -> list[str]:
        return sorted(n for n, p in self._processes.items() if p.returncode is None)

    def status(self) -> dict[str, Any]:
        return {
            name: {
                "driver": launch.robot.driver,
                "running": name in self.running,
                "restarts": self._failures.get(name, 0),
            }
            for name, launch in sorted(self._wanted.items())
        }

    async def sync(self, wanted: Sequence[Launch]) -> None:
        by_name = {w.name: w for w in wanted}
        for name in list(self._processes):
            current = self._wanted.get(name)
            if name not in by_name or (current is not None and current != by_name[name]):
                await self._stop(name)
        self._wanted = by_name
        await self.tick()

    async def tick(self) -> None:
        """Start what should run and is not, after its backoff."""
        for name, launch in self._wanted.items():
            process = self._processes.get(name)
            if process is not None and process.returncode is None:
                continue
            backoff = self._backoffs.setdefault(name, RestartBackoff(1.0, 30.0, self._clock))
            if process is not None:  # it exited: count it and back off
                self._failures[name] = self._failures.get(name, 0) + 1
                delay = backoff.exited()
                self._restart_at[name] = self._clock() + delay
                del self._processes[name]
                log.warning(
                    "robot %s exited (%s); restarting in %ss", name, process.returncode, delay
                )
                continue
            if self._clock() < self._restart_at.get(name, 0.0):
                continue
            self.log_dir.mkdir(parents=True, exist_ok=True)
            output = (self.log_dir / f"{name}.log").open("ab")
            try:
                self._processes[name] = await asyncio.create_subprocess_exec(
                    *launch.command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    env={**os.environ, **launch.env},
                )
                backoff.started()
            except OSError as error:
                log.error("cannot start robot %s: %s", name, error)
                self._restart_at[name] = self._clock() + backoff.max_s
            finally:
                output.close()

    async def _stop(self, name: str) -> None:
        process = self._processes.pop(name, None)
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    async def stop(self) -> None:
        for name in list(self._processes):
            await self._stop(name)
        self._wanted = {}
