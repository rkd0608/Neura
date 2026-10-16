"""The robot cell (``aloha.cell``): its controller's tasks around the robot process, and its
page's guards. The end-to-end run, with a site agent, is tools/smoke_site_kit.py."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("MUJOCO_GL", "egl")
pytest.importorskip("mujoco")
pytest.importorskip("PIL")

from neura_contracts import RobotProfile, parse_document  # noqa: E402
from neura_robots.aloha import AlohaSim  # noqa: E402
from neura_robots.aloha.arm import AlohaSimArm  # noqa: E402
from neura_robots.aloha.cell import Cell, CellRobot, serve_page  # noqa: E402
from neura_robots.aloha.contracts import robot_profile  # noqa: E402
from neura_runtime.grpc_client import RobotCredentials  # noqa: E402
from neura_runtime.runtime import RuntimeState  # noqa: E402


def _renders() -> bool:
    sim = AlohaSim()
    try:
        sim.render("top")
        return True
    except Exception:
        return False
    finally:
        sim.close()


if not _renders():
    pytest.skip("no offscreen OpenGL (install libegl1, or set MUJOCO_GL)", allow_module_level=True)


class FakeProcess:
    """What the controller reads of the robot process (``process.ArmProcess``)."""

    def __init__(self, revision: str = "rev_1") -> None:
        self.revision_id = revision
        self.runtime = SimpleNamespace(
            state=RuntimeState.READY, session_id="ses_1", fault=None, counters=Counter()
        )
        self.client = SimpleNamespace(
            rtt=SimpleNamespace(p99_us=lambda: 12_000), last_rejection=None, last_rejection_detail=""
        )
        self.connection = SimpleNamespace(late_ticks=0)


def _robot(**kwargs: Any) -> CellRobot:
    robot = CellRobot(
        "aloha-1",
        RobotCredentials(token="dev"),
        gateway="127.0.0.1:1",
        max_steps=20,
        first_seed=1000,
        pause_s=1.0,
        say=lambda line: None,
        **kwargs,
    )
    profile = parse_document(robot_profile())
    assert isinstance(profile, RobotProfile)
    robot.arm = AlohaSimArm(robot.world, profile)
    robot.arm.connect()
    return robot


def _commands(robot: CellRobot) -> list[str]:
    fresh, _ = robot.inbox.take()
    return fresh


def test_tasks_run_back_to_back_with_a_pause_between() -> None:
    robot = _robot()
    process = FakeProcess()
    robot.assigned.process = process  # type: ignore[assignment]
    robot.step(0.0)
    assert robot.task is not None and robot.task.seed == 1000
    assert _commands(robot) == ["arm"]

    process.runtime.state = RuntimeState.RUNNING
    robot.step(0.1)
    assert robot.task.started
    for _ in range(20):  # the world's time runs out (its thread is not started here)
        robot.world.sim.advance()
    robot.step(0.2)
    assert robot.task is None
    assert _commands(robot) == ["stop"]
    (outcome,) = robot.outcomes
    assert (outcome.seed, outcome.result, outcome.revision) == (1000, "timeout", "rev_1")

    process.runtime.state = RuntimeState.READY
    robot.step(0.5)  # still pausing between tasks
    assert robot.task is None
    robot.step(1.3)
    assert robot.task is not None and robot.task.seed == 1001
    status = robot.status()
    assert status["tasks"] == 1 and status["task"]["seed"] == 1001
    assert status["rtt_p99_ms"] == 12.0
    robot.world.close()


def test_no_task_starts_while_paused_or_before_a_new_revision_is_taken() -> None:
    robot = _robot()
    robot.assigned.process = FakeProcess()  # type: ignore[assignment]
    robot.command("pause")
    robot.step(0.0)
    assert robot.task is None
    robot.command("resume")
    robot.assigned.pending = "rev_2"
    robot.step(0.1)
    assert robot.task is None  # the robot switches first, at this boundary
    robot.assigned.pending = None
    robot.step(0.2)
    assert robot.task is not None
    robot.world.close()


def test_a_fault_ends_the_task_and_is_reset_only_when_configured() -> None:
    robot = _robot(reset_after_s=5.0)
    process = FakeProcess()
    robot.assigned.process = process  # type: ignore[assignment]
    robot.step(0.0)
    process.runtime.state = RuntimeState.RUNNING
    robot.step(0.1)
    process.runtime.state = RuntimeState.FAULTED
    process.runtime.fault = SimpleNamespace(
        reason=SimpleNamespace(value="STARVATION_GRACE_EXHAUSTED"), detail=""
    )
    robot.step(0.2)
    assert robot.outcomes[-1].result == "fault"
    assert robot.outcomes[-1].detail == "STARVATION_GRACE_EXHAUSTED"
    _commands(robot)
    robot.step(3.0)
    assert _commands(robot) == []
    assert robot.status()["reset_in_s"] == 2
    robot.step(5.3)
    assert _commands(robot) == ["reset"]
    robot.world.close()


def test_the_page_serves_status_and_frames_and_takes_actions_only_from_itself(
    tmp_path: Any,
) -> None:
    cell = Cell(
        tmp_path,
        gateway="127.0.0.1:1",
        tls_server_name=None,
        task="transfer_cube",
        max_steps=20,
        pause_s=1.0,
        reset_after_s=None,
        site=None,
        say=lambda line: None,
    )
    robot = _robot()
    robot.camera.start()
    cell.robots["aloha-1"] = robot
    robot.snapshot = robot.status()
    server = serve_page(cell, "127.0.0.1:0", [])
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> Any:
        request = urllib.request.Request(base + path, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers["Content-Type"], response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers["Content-Type"], exc.read()

    try:
        code, _, body = call("/api/cell")
        assert code == 200
        assert [r["name"] for r in json.loads(body)["robots"]] == ["aloha-1"]
        code, kind, frame = call("/robots/aloha-1/frame.jpg?camera=top")
        assert (code, kind) == (200, "image/jpeg")
        assert frame[:2] == b"\xff\xd8"
        assert call("/robots/aloha-9/frame.jpg")[0] == 404
        assert call("/robots/aloha-1/frame.jpg?camera=../etc")[0] == 404

        # Actions: only from this page (a header other sites' pages cannot send), and only to
        # its own names (no DNS rebinding).
        assert call("/api/robots/aloha-1/pause", method="POST")[0] == 403
        assert not robot.paused
        rebound = {"X-Neura-Cell": "1", "Host": "attacker.example"}
        assert call("/api/robots/aloha-1/pause", method="POST", headers=rebound)[0] == 403
        ok = call("/api/robots/aloha-1/pause", method="POST", headers={"X-Neura-Cell": "1"})
        assert ok[0] == 200 and robot.paused
        assert call("/api/site", headers={"Host": "attacker.example"})[0] == 421
        assert call("/api/site")[0] == 404  # no site API configured
    finally:
        server.shutdown()
        server.server_close()
        robot.close()
        robot.world.close()
