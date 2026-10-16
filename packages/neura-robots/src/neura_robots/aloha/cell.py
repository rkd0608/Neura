"""``neura-aloha-cell``: a robot cell of simulated ALOHAs served by a Neura site, running their
task back to back, and a page to watch them.

Every robot is the simulated ALOHA (``aloha.arm``) behind the real robot runtime and robot-side
process (``process.AssignedArm``), on its own thread as on its own computer: it connects to the
site gateway with mutual TLS, runs the revision the site assigns it, and switches revisions
between tasks, never during one. The cell's controller does what a cell's PLC and its operator
do: when a robot is ready it resets the scene (the cube somewhere new) and starts a task; when
the task is done (the cube handed over, or the time is up) it ends it. A fault latches: the robot
holds until it is reset, from the page, or by the cell after ``--reset-after`` seconds.

Robots: one per key and certificate in ``--cert-dir`` (``<robot>.key`` and ``<robot>.pem``, with
the site CA in ``ca.pem``, as the site issues them), including robots added to the site later.

The page (``--listen``): each robot's camera, live; its state, revision, tasks and successes;
and, with ``--site-api``, the site agent's connection to Neura and the activations waiting for
the site operator's approval, approved with the operator credential in
``NEURA_AGENT_OPERATOR_TOKEN``. It has no sign-in: listen on loopback, or behind something
that authenticates.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import io
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("MUJOCO_GL", "egl")

import mujoco
import numpy as np
from numpy.typing import NDArray

from neura_contracts import RobotProfile
from neura_robots.aloha import AlohaSim, Task
from neura_robots.aloha.arm import AlohaSimArm
from neura_robots.aloha.world import RealTimeWorld
from neura_robots.process import AssignedArm, OperatorInbox
from neura_runtime.grpc_client import RobotCredentials
from neura_runtime.runtime import ACTIVE_STATES, RuntimeState

log = logging.getLogger("neura.cell")
Say = Callable[[str], None]

# Cameras an onlooker can watch; "top" is the one the policy sees.
VIEW_CAMERAS = ("angle", "top", "front_close", "left_pillar", "right_pillar")
ACTIONS = ("pause", "resume", "takeover", "reset")


@dataclass(frozen=True, slots=True)
class Outcome:
    seed: int
    result: str  # success | timeout | fault | stopped
    steps: int
    revision: str
    detail: str = ""
    at: float = field(default_factory=time.time)


@dataclass(slots=True)
class _Task:
    seed: int
    process: Any
    revision: str
    submitted_at: float
    started: bool = False


class Camera:
    """An onlooker's view of a robot's scene: its own renderer on its own thread, drawing (at
    ``fps``) the cameras someone asked for in the last few seconds, and nothing while nobody
    watches. Shadows and reflections are off: the view is for people, not for the policy."""

    def __init__(
        self, world: RealTimeWorld, *, fps: float = 10.0, width: int = 640, height: int = 480
    ) -> None:
        self.world = world
        self.fps = fps
        self.width = width
        self.height = height
        self._wanted: dict[str, float] = {}
        self._frames: dict[str, tuple[int, bytes]] = {}
        self._changed = threading.Condition()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> Camera:
        self._running = True
        self._thread = threading.Thread(target=self._run, name="cell-camera", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        with self._changed:
            self._running = False
            self._changed.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def latest(self, camera: str, *, after: int = 0, timeout: float = 2.0) -> tuple[int, bytes]:
        """The newest frame (its number and JPEG), waiting up to ``timeout`` for one newer than
        ``after``; (0, b"") when there is none yet."""
        with self._changed:
            self._wanted[camera] = time.monotonic()
            self._changed.wait_for(
                lambda: self._frames.get(camera, (0, b""))[0] > after or not self._running,
                timeout,
            )
            return self._frames.get(camera, (0, b""))

    def _run(self) -> None:
        from PIL import Image

        model = self.world.sim.model
        renderer = mujoco.Renderer(model, self.height, self.width)
        data = mujoco.MjData(model)
        try:
            while self._running:
                started = time.monotonic()
                with self._changed:
                    cameras = [c for c, at in self._wanted.items() if started - at < 5.0]
                if cameras:
                    self.world.copy_state(data)
                    for camera in cameras:
                        renderer.update_scene(data, camera=camera)
                        renderer.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = False
                        renderer.scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION] = False
                        image: NDArray[np.uint8] = renderer.render()
                        encoded = io.BytesIO()
                        Image.fromarray(image).save(encoded, "JPEG", quality=82)
                        with self._changed:
                            number = self._frames.get(camera, (0, b""))[0] + 1
                            self._frames[camera] = (number, encoded.getvalue())
                            self._changed.notify_all()
                time.sleep(max(0.01, 1 / self.fps - (time.monotonic() - started)))
        finally:
            renderer.close()


class CellRobot:
    """One simulated ALOHA, its robot process and the cell's controller for it."""

    def __init__(
        self,
        name: str,
        credentials: RobotCredentials,
        *,
        gateway: str,
        task: Task = "transfer_cube",
        max_steps: int = 400,
        first_seed: int = 0,
        pause_s: float = 2.0,
        reset_after_s: float | None = None,
        say: Say = print,
        view_fps: float = 10.0,
    ) -> None:
        self.name = name
        self.world = RealTimeWorld(AlohaSim(task))
        self.world.reset(first_seed)  # the scene, still, until the first task
        self.inbox = OperatorInbox()
        self.arm: AlohaSimArm | None = None
        self.assigned = AssignedArm(
            self._open_arm,
            name,
            gateway=gateway,
            credentials=credentials,
            inbox=self.inbox,
            say=self._say,
        )
        self.max_steps = max_steps
        self.next_seed = first_seed
        self.pause_s = pause_s
        self.reset_after_s = reset_after_s
        self.paused = False
        self.task: _Task | None = None
        self.outcomes: deque[Outcome] = deque(maxlen=1000)
        self.lines: deque[tuple[float, str]] = deque(maxlen=40)
        self.camera = Camera(self.world, fps=view_fps)
        self.snapshot: dict[str, Any] = {"name": name, "state": "Starting"}
        self._print = say
        self._faulted_since: float | None = None
        self._resume_at = 0.0
        self._now = 0.0  # the controller's clock, at its last step

    # --- the robot's computer --------------------------------------------------------------------

    def _say(self, line: str) -> None:
        text = line.removeprefix(f"{self.name}: ")
        self._print(f"{self.name}: {text}")
        # The robot process's hints are for a terminal ("- type arm to start a task").
        self.lines.append((time.time(), text.split(" - type ", 1)[0]))

    def _open_arm(self, profile: RobotProfile) -> AlohaSimArm:
        arm = AlohaSimArm(self.world, profile)
        arm.connect()
        self.arm = arm
        return arm

    def run(self, stop: threading.Event) -> None:
        """The robot's control loop and the cell's controller for it, on this thread."""
        asyncio.run(self._main(stop))

    async def _main(self, stop: threading.Event) -> None:
        done = asyncio.Event()

        async def watch() -> None:
            while not stop.is_set():
                await asyncio.sleep(0.2)
            done.set()

        self.world.start()
        try:
            await asyncio.gather(watch(), self.assigned.run(done), self._control(done))
        finally:
            if self.arm is not None:
                self.arm.close()  # holds, and stops the world
            else:
                self.world.close()

    # --- the cell's controller -------------------------------------------------------------------

    async def _control(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                self.step(time.monotonic())
                self.snapshot = self.status()
            except Exception:
                log.exception("%s: cell controller", self.name)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 0.05)

    def step(self, now: float) -> None:
        self._now = now
        process = self.assigned.process
        if process is None:
            return
        runtime = process.runtime
        state = runtime.state
        task = self.task
        if task is not None:
            self._follow(task, process, state, now)
            return
        if state is RuntimeState.FAULTED:
            if self._faulted_since is None:
                self._faulted_since = now
            elif self.reset_after_s is not None and now - self._faulted_since >= self.reset_after_s:
                self.inbox.submit("reset")
                self._faulted_since = now  # again after another period, if it was refused
            return
        self._faulted_since = None
        if self.paused or now < self._resume_at or self.assigned.pending is not None:
            return  # a new revision is assigned: the robot switches before the next task
        if state is RuntimeState.READY and runtime.session_id is not None and self.arm is not None:
            seed = self.next_seed
            self.next_seed += 1
            self.world.reset(seed, self.max_steps)
            self.arm.recover()
            self.world.go()
            self.inbox.submit("arm")
            self.task = _Task(seed, process, process.revision_id, now)

    def _follow(self, task: _Task, process: Any, state: RuntimeState, now: float) -> None:
        if task.process is not process:
            self.world.stop()
            self._end(task, "stopped", "the robot switched revisions")
        elif not task.started:
            if state in ACTIVE_STATES:
                task.started = True
            elif state is RuntimeState.FAULTED:
                self._end(task, "fault", self._fault(process))
                self._faulted_since = now
            elif now - task.submitted_at > 3.0:
                # The arm command found the robot not ready (its session was being renewed,
                # say): not a task. Try again.
                self.world.stop()
                self.task = None
                self.next_seed = task.seed
        elif state is RuntimeState.FAULTED:
            # The world goes on (until the task's time is up) while the robot holds.
            self._end(task, "fault", self._fault(process))
            self._faulted_since = now
        elif self.world.done:
            self.world.stop()
            if state in ACTIVE_STATES:
                self.inbox.submit("stop")
            self._end(task, "success" if self.world.sim.success else "timeout")
            self._resume_at = now + self.pause_s
        elif state not in ACTIVE_STATES:
            self.world.stop()
            self._end(task, "stopped", "taken over by the operator")
            self._resume_at = now + self.pause_s

    @staticmethod
    def _fault(process: Any) -> str:
        fault = process.runtime.fault
        if fault is None:
            return ""
        return str(fault.reason.value) + (f": {fault.detail}" if fault.detail else "")

    def _end(self, task: _Task, result: str, detail: str = "") -> None:
        self.outcomes.append(Outcome(task.seed, result, self.world.steps, task.revision, detail))
        self.task = None
        self._say(f"task {task.seed}: {result}" + (f" ({detail})" if detail else ""))

    def command(self, action: str) -> str:
        """An operator's action from the page."""
        if action == "pause":
            self.paused = True
            return "no new task starts; the current one finishes"
        if action == "resume":
            self.paused = False
            return "tasks start again"
        if action in ("takeover", "reset"):
            self.inbox.submit(action)
            return f"{action} sent"
        raise ValueError(f"unknown action {action!r}")

    def status(self) -> dict[str, Any]:
        process = self.assigned.process
        runtime = process.runtime if process is not None else None
        outcomes = list(self.outcomes)
        body: dict[str, Any] = {
            "name": self.name,
            "state": runtime.state.value if runtime is not None else "Unassigned",
            "revision": process.revision_id if process is not None else None,
            "pending_revision": self.assigned.pending,
            "refused_revision": (
                self.assigned.refused
                if self.assigned.refused and not self.assigned.refused.startswith("waiting:")
                else None
            ),
            "paused": self.paused,
            "tasks": len(outcomes),
            "successes": sum(o.result == "success" for o in outcomes),
            "recent": [dataclasses.asdict(o) for o in outcomes[-30:]],
            "lines": [{"at": at, "text": text} for at, text in list(self.lines)[-8:]],
        }
        if self.task is not None:
            body["task"] = {
                "seed": self.task.seed,
                "steps": self.world.steps,
                "max_steps": self.max_steps,
                "reward": self.world.sim.max_reward_seen,
            }
        if runtime is not None and process is not None:
            client = process.client
            body |= {
                "session": runtime.session_id is not None,
                "chunks": int(runtime.counters.get("chunks_accepted", 0)),
                "rtt_p99_ms": round(client.rtt.p99_us() / 1000, 1),
                "late_ticks": process.connection.late_ticks,
            }
            if runtime.session_id is None and client.last_rejection is not None:
                body["refusal"] = f"{client.last_rejection.value}: {client.last_rejection_detail}"
            if runtime.state is RuntimeState.FAULTED:
                body["fault"] = self._fault(process)
                if self.reset_after_s is not None and self._faulted_since is not None:
                    left = self.reset_after_s - (self._now - self._faulted_since)
                    body["reset_in_s"] = max(0, round(left))
        return body

    def close(self) -> None:
        self.camera.stop()


class SiteApi:
    """The site agent's local API: its status, and the activations waiting for the site
    operator."""

    def __init__(self, url: str, operator_token: str | None, operator: str) -> None:
        self.url = url.rstrip("/")
        self.token = operator_token
        self.operator = operator

    def _call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if self.token and method != "GET":
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(  # noqa: S310 - a configured http(s) URL
            self.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {"message": str(exc)}
        except (OSError, ValueError) as exc:
            return 502, {"message": f"the site agent did not answer: {exc}"}

    def status(self) -> tuple[int, Any]:
        code, status = self._call("GET", "/v1/local/status")
        if code != 200:
            return code, status
        return 200, {
            "enrolled": status.get("enrolled", False),
            "identity": status.get("identity"),
            "connected": (status.get("connection") or {}).get("connected", False),
            "last_error": (status.get("connection") or {}).get("last_error"),
            "offline_authorization": status.get("offline_authorization"),
            "pending_approvals": status.get("pending_approvals", []),
            "can_approve": bool(self.token),
        }

    def decide(self, command_id: str, decision: str) -> tuple[int, Any]:
        if not self.token:
            return 403, {"message": "set NEURA_AGENT_OPERATOR_TOKEN to approve from this page"}
        body: dict[str, str] = {"operator": self.operator}
        if decision == "refuse":
            body["note"] = "refused on the cell page"
        return self._call("POST", f"/v1/local/approvals/{command_id}/{decision}", body)


class Cell:
    def __init__(
        self,
        cert_dir: Path,
        *,
        gateway: str,
        tls_server_name: str | None,
        task: Task,
        max_steps: int,
        pause_s: float,
        reset_after_s: float | None,
        site: SiteApi | None,
        say: Say = print,
        view_fps: float = 10.0,
    ) -> None:
        self.cert_dir = cert_dir
        self.gateway = gateway
        self.tls_server_name = tls_server_name
        self.task: Task = task
        self.max_steps = max_steps
        self.pause_s = pause_s
        self.reset_after_s = reset_after_s
        self.site = site
        self.say = say
        self.view_fps = view_fps
        self.robots: dict[str, CellRobot] = {}
        self.stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def names(self) -> list[str]:
        if not self.cert_dir.is_dir() or not (self.cert_dir / "ca.pem").is_file():
            return []
        return sorted(
            p.stem
            for p in self.cert_dir.glob("*.pem")
            if p.name != "ca.pem" and p.with_suffix(".key").is_file()
        )

    def discover(self) -> None:
        for name in self.names():
            if name in self.robots:
                continue
            try:
                loaded = RobotCredentials.from_directory(self.cert_dir, name)
            except OSError as exc:
                log.warning("%s: cannot read its credentials yet: %s", name, exc)
                continue
            credentials = dataclasses.replace(loaded, server_name=self.tls_server_name)
            robot = CellRobot(
                name,
                credentials,
                gateway=self.gateway,
                task=self.task,
                max_steps=self.max_steps,
                # Each robot has its own scenes; seeds are reproducible per robot.
                first_seed=1000 * (len(self.robots) + 1),
                pause_s=self.pause_s,
                reset_after_s=self.reset_after_s,
                say=self.say,
                view_fps=self.view_fps,
            )
            robot.camera.start()
            thread = threading.Thread(target=robot.run, args=(self.stop,), name=f"robot-{name}")
            thread.start()
            self.robots[name] = robot
            self._threads.append(thread)
            self.say(f"{name}: connecting to the site gateway at {self.gateway}")

    def run(self) -> None:
        while not self.stop.is_set():
            self.discover()
            self.stop.wait(2.0)
        for thread in self._threads:
            thread.join(timeout=10)
        for robot in self.robots.values():
            robot.close()

    def overview(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "gateway": self.gateway,
            "waiting_for_certificates": not self.robots,
            "robots": [self.robots[name].snapshot for name in sorted(self.robots)],
        }


def _page() -> bytes:
    return resources.files("neura_robots.aloha").joinpath("cell.html").read_bytes()


class _Handler(BaseHTTPRequestHandler):
    cell: ClassVar[Cell]
    allowed_hosts: ClassVar[frozenset[str]]
    server_version = "neura-aloha-cell"

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s " + format, self.address_string(), *args)

    def _host_allowed(self) -> bool:
        """Only requests addressed to this page's own names (against DNS rebinding). A name
        starting with a dot allows its subdomains (".cloudshell.dev": Cloud Shell's web preview
        of a page forwarded from a site in the cloud)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in self.allowed_hosts or any(
            name.startswith(".") and host.endswith(name) for name in self.allowed_hosts
        )

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, body: Any) -> None:
        self._send(code, json.dumps(body).encode(), "application/json")

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._json(421, {"message": "unknown host"})
            return
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if url.path == "/":
            self._send(200, _page(), "text/html; charset=utf-8")
        elif url.path == "/api/cell":
            self._json(200, self.cell.overview())
        elif url.path == "/api/site":
            if self.cell.site is None:
                self._json(404, {"message": "no site API configured (--site-api)"})
            else:
                self._json(*self.cell.site.status())
        elif len(parts) == 3 and parts[0] == "robots" and parts[2] in ("stream", "frame.jpg"):
            robot = self.cell.robots.get(parts[1])
            camera = parse_qs(url.query).get("camera", ["angle"])[0]
            if robot is None or camera not in VIEW_CAMERAS:
                self._json(404, {"message": "no such robot or camera"})
            elif parts[2] == "frame.jpg":
                _, frame = robot.camera.latest(camera, timeout=5)
                if frame:
                    self._send(200, frame, "image/jpeg")
                else:
                    self._json(503, {"message": "no frame yet"})
            else:
                self._stream(robot.camera, camera)
        else:
            self._json(404, {"message": "not found"})

    def _stream(self, camera: Camera, name: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seen = 0
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, TimeoutError):
            while not self.cell.stop.is_set():
                number, frame = camera.latest(name, after=seen, timeout=5)
                if number <= seen or not frame:
                    continue
                seen = number
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                    + frame
                    + b"\r\n"
                )
                self.wfile.flush()

    def do_POST(self) -> None:
        # A custom header no other site's page can send without a CORS preflight (which this
        # server never grants): the operator's actions come from this page only.
        if not self._host_allowed() or self.headers.get("X-Neura-Cell") != "1":
            self._json(403, {"message": "actions come from the cell page"})
            return
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 4 and parts[:2] == ["api", "robots"] and parts[3] in ACTIONS:
            robot = self.cell.robots.get(parts[2])
            if robot is None:
                self._json(404, {"message": "no such robot"})
            else:
                self._json(200, {"message": robot.command(parts[3])})
        elif (
            len(parts) == 5
            and parts[:3] == ["api", "site", "approvals"]
            and parts[4] in ("approve", "refuse")
        ):
            if self.cell.site is None:
                self._json(404, {"message": "no site API configured (--site-api)"})
            else:
                self._json(*self.cell.site.decide(parts[3], parts[4]))
        else:
            self._json(404, {"message": "not found"})


def serve_page(cell: Cell, listen: str, allowed_hosts: list[str]) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    hosts = {"localhost", "127.0.0.1", "::1", *(h.lower() for h in allowed_hosts)}
    handler = type("CellHandler", (_Handler,), {"cell": cell, "allowed_hosts": frozenset(hosts)})
    server = ThreadingHTTPServer((host.strip("[]") or "0.0.0.0", int(port)), handler)  # noqa: S104
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="cell-page", daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(
        prog="neura-aloha-cell",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gateway", default=env("NEURA_CELL_GATEWAY", "127.0.0.1:50051"))
    parser.add_argument(
        "--cert-dir",
        type=Path,
        default=Path(env("NEURA_CELL_CERT_DIR", "/var/lib/neura/robot-certs")),
        help="the robots' keys and certificates from the site CA (one robot per certificate)",
    )
    parser.add_argument(
        "--tls-server-name",
        default=env("NEURA_CELL_TLS_SERVER_NAME"),
        help="the gateway name to verify, when --gateway is an address its certificate omits",
    )
    parser.add_argument(
        "--task",
        choices=("transfer_cube", "insertion"),
        default=env("NEURA_CELL_TASK", "transfer_cube"),
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=int(env("NEURA_CELL_MAX_STEPS", "400")),
        help="the time a task may take, in 20 ms control periods (LeRobot's evaluation: 400)",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=float(env("NEURA_CELL_PAUSE_S", "2")),
        help="seconds between tasks",
    )
    parser.add_argument(
        "--reset-after",
        type=float,
        default=float(env("NEURA_CELL_RESET_AFTER_S", "0")) or None,
        help="reset a latched fault after this many seconds, as an operator would (default: "
        "never; the page's Reset)",
    )
    parser.add_argument("--listen", default=env("NEURA_CELL_LISTEN", "127.0.0.1:8095"))
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[h for h in env("NEURA_CELL_ALLOW_HOSTS", "").split(",") if h],
        help="another name the page is reached by (localhost and 127.0.0.1 always are); "
        ".example.com allows its subdomains",
    )
    parser.add_argument("--view-fps", type=float, default=float(env("NEURA_CELL_VIEW_FPS", "10")))
    parser.add_argument(
        "--site-api",
        default=env("NEURA_CELL_SITE_API"),
        help="the site agent's local API, e.g. http://127.0.0.1:8090",
    )
    parser.add_argument(
        "--operator",
        default=env("NEURA_CELL_OPERATOR", "site operator (cell page)"),
        help="who approves activations from the page, for the site's audit log",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=env("NEURA_LOG_LEVEL", "warning").upper())

    printing = threading.Lock()

    def say(line: str) -> None:
        with printing:  # robots speak from their own threads
            print(line, flush=True)

    site = (
        SiteApi(args.site_api, env("NEURA_AGENT_OPERATOR_TOKEN") or None, args.operator)
        if args.site_api
        else None
    )
    cell = Cell(
        args.cert_dir,
        gateway=args.gateway,
        tls_server_name=args.tls_server_name,
        task=args.task,
        max_steps=args.max_steps,
        pause_s=args.pause,
        reset_after_s=args.reset_after,
        site=site,
        say=say,
        view_fps=args.view_fps,
    )
    server = serve_page(cell, args.listen, args.allow_host)
    say(f"the cell's page: http://{args.listen.replace('0.0.0.0', '127.0.0.1')}/")  # noqa: S104
    if not cell.names():
        say(f"waiting for robot certificates in {args.cert_dir} (the site issues them)")

    def interrupted(*_: Any) -> None:
        cell.stop.set()

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        cell.run()
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
