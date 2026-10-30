"""The simulated ALOHA as a robot integration, its contracts, and the evidence runners on a
synthetic (random-weights) checkpoint of the published ACT checkpoints' shape."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("MUJOCO_GL", "egl")
pytest.importorskip("mujoco")


def _renders() -> bool:
    from neura_robots.aloha import AlohaSim

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

from neura_contracts import FaultResponse, RobotProfile, parse_document  # noqa: E402
from neura_robots.aloha import JOINTS, AlohaSim  # noqa: E402
from neura_robots.aloha.arm import AlohaSimArm  # noqa: E402
from neura_robots.aloha.contracts import Chunking, robot_profile, write  # noqa: E402


def _robot() -> RobotProfile:
    robot = parse_document(robot_profile())
    assert isinstance(robot, RobotProfile)
    return robot


@pytest.fixture
def arm() -> AlohaSimArm:
    sim = AlohaSim()
    sim.reset(1000)
    arm = AlohaSimArm(sim, _robot())
    arm.connect()
    return arm


def test_the_profile_fits_the_arm(arm: AlohaSimArm) -> None:
    assert arm.contract_problems() == []
    document = robot_profile()
    document["spec"]["command_interface"][0]["unit"] = "deg"
    document["spec"]["command_interface"][1]["max"] = 9.0
    document["spec"]["cameras"][0]["width"] = 320
    wrong = parse_document(document)
    assert isinstance(wrong, RobotProfile)
    problems = arm.contract_problems(wrong)
    assert any("unit deg" in p for p in problems)
    assert any("actuator takes" in p for p in problems)
    assert any("320x480" in p for p in problems)


def test_commands_set_targets_and_time_is_the_ticks(arm: AlohaSimArm) -> None:
    target = arm.sim.qpos()
    target[0] += 0.2
    assert arm.command(target).ok
    assert arm.sim.steps == 0  # a command does not advance time
    for _ in range(50):
        arm.advance()
    assert arm.sim.steps == 50
    assert arm.sim.qpos()[0] == pytest.approx(target[0], abs=0.02)
    assert arm.counters["commands"] == 1


def test_hold_and_faults(arm: AlohaSimArm) -> None:
    moving = arm.sim.qpos()
    moving[1] += 0.3
    arm.command(moving)
    for _ in range(3):
        arm.advance()
    assert arm.apply_response(FaultResponse.HOLD_POSITION)
    held = arm.sim.qpos()
    for _ in range(25):
        arm.advance()
    assert np.max(np.abs(arm.sim.qpos() - held)[[0, 1, 2, 7, 8, 9]]) < 0.05

    arm.fail_commands = True
    assert not arm.command(held).ok
    arm.fail_commands = False
    arm.release_authority()
    assert not arm.has_authority()
    assert arm.revalidate() == ["this process no longer holds the actuator: restart it"]


def test_capture_and_camera_faults(arm: AlohaSimArm) -> None:
    snapshot = arm.capture(1_000)
    assert list(snapshot.state) == JOINTS
    frame = snapshot.images["top"]
    assert (frame.width, frame.height, len(frame.data)) == (640, 480, 640 * 480 * 3)
    arm.camera_fault = "frozen"
    arm.advance()
    frozen = arm.capture(2_000).images["top"]
    assert frozen.capture_time_us == 1_000  # the old frame, with its old time
    arm.camera_fault = "unplugged"
    assert "top" not in arm.capture(3_000).images
    assert arm.counters["camera_top_no_frame"] == 1


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("lerobot")
    from neura_robots.evidence.synthetic import write_checkpoint

    return write_checkpoint(tmp_path_factory.mktemp("ckpt") / "checkpoint", size="tiny")


def test_contracts_bind_to_the_checkpoint(synthetic: Path, tmp_path: Path) -> None:
    deployment = write(
        tmp_path, synthetic, name="act-aloha", source="synthetic", chunking=Chunking(refill_at=20)
    )
    binding = deployment.binding
    assert binding.chunk_length == 100
    assert binding.action_dim == 14
    assert binding.profile.spec.chunk.refill_threshold_actions == 20
    assert {w.path for w in binding.artifact.spec.weights} >= {"config.json", "model.safetensors"}


def test_local_baselines_run(synthetic: Path) -> None:
    from neura_robots.aloha.world import RealTimeWorld
    from neura_robots.evidence.local import run_eval_protocol, run_realtime
    from neura_robots.evidence.policy import LocalPolicy

    policy = LocalPolicy(synthetic)
    sim = AlohaSim()
    (result,) = run_eval_protocol(policy, sim, [1000], max_steps=120)
    assert result.steps == 120
    assert not result.success  # random weights
    assert len(result.inference_ms) == 2  # chunks of 100: at steps 0 and 100
    world = RealTimeWorld(AlohaSim()).start()
    (result,) = run_realtime(policy, world, [1000], max_steps=60)
    world.close()
    assert result.steps == 60
    assert result.system == "lerobot-realtime"


def test_neura_serves_episodes(synthetic: Path, tmp_path: Path) -> None:
    from neura_robots.evidence.served import ServedRun

    deployment = write(tmp_path / "contracts", synthetic, name="act-aloha", source="synthetic")
    for realtime in (True, False):
        run = ServedRun(deployment, workdir=tmp_path / f"site-{realtime}", realtime=realtime)
        (result,) = asyncio.run(run.episodes([1000], max_steps=150))
        assert result.steps == 150
        assert result.faults == []
        assert result.counters["chunks_accepted"] >= 2
        assert result.counters["max_observation_age_at_acceptance_ms"] > 0
        assert run.pairs  # inputs and served actions, for parity


def test_a_summary_counts_fault_reasons_and_takes_counter_medians() -> None:
    from neura_robots.evidence.results import EpisodeResult, summarize

    results = [
        EpisodeResult("neura", 1000, True, 4, 300, counters={"late_ticks": 2}),
        EpisodeResult("neura", 1001, False, 1, 400, faults=["STARVATION_GRACE_EXHAUSTED"] * 2),
        EpisodeResult("neura", 1002, False, 0, 400, counters={"late_ticks": 6}, faults=["X"]),
    ]
    summary = summarize(results)
    assert summary["faulted_episodes"] == 2
    assert summary["faults"] == {"STARVATION_GRACE_EXHAUSTED": 1, "X": 1}  # episodes, not repeats
    assert summary["counter_medians"] == {"late_ticks": 2}  # 0, 2 and 6


def test_the_summary_table_says_what_a_system_was_told(capsys: pytest.CaptureFixture[str]) -> None:
    from neura_robots.evidence.cli import _table
    from neura_robots.evidence.results import EpisodeResult, summarize

    refused = {"REQUEST_EXPIRED": 380, "late_ticks": 1}
    results = [EpisodeResult("neura", 1000 + i, False, 0, 400, counters=refused) for i in range(3)]
    _table({"neura": {"summary": summarize(results)}})
    out = capsys.readouterr().out
    assert "0/3 succeeded" in out
    assert "medians: late_ticks 1" in out
    assert "reason codes, medians: REQUEST_EXPIRED 380" in out


def test_the_async_robots_camera_reads_like_lerobots() -> None:
    from neura_robots.aloha.world import RealTimeWorld
    from neura_robots.evidence.lerobot_async import _Camera

    world = RealTimeWorld(AlohaSim()).start()
    camera = _Camera(world, max_age_s=10).start()
    try:
        world.reset(1000)
        camera.invalidate()
        first = camera.read()
        assert camera.waits == 1  # none drawn yet: it waits for one
        assert np.array_equal(first, world.render("top"))
        camera.read()
        assert camera.waits == 1  # the newest is recent: no wait for the renderer
        world.reset(1001)  # the cube elsewhere
        camera.invalidate()
        moved = camera.read()
        assert camera.waits == 1  # counted afresh, and waited: no frame of the last episode
        assert np.array_equal(moved, world.render("top"))
        assert not np.array_equal(moved, first)
    finally:
        camera.stop()
        world.close()


def test_lerobots_async_inference_serves_an_episode(synthetic: Path, tmp_path: Path) -> None:
    from neura_robots.aloha.world import RealTimeWorld
    from neura_robots.evidence.lerobot_async import AsyncRun

    world = RealTimeWorld(AlohaSim()).start()
    try:
        # Random weights barely move the arm: no observation is skipped as too similar.
        with AsyncRun(synthetic, world, workdir=tmp_path, similarity_atol=1e-9) as run:
            result = run.episode(1000, max_steps=100)
    finally:
        world.close()
    assert result.steps == 100
    assert result.system == "lerobot-async-scaled"
    assert result.counters["chunks_received"] >= 1
    assert result.counters["actions_executed"] > 0
    assert result.counters["camera_waits"] >= 1  # the episode's first frame
    assert result.counters["control_ticks"] >= result.counters["actions_executed"]
    assert result.counters["server_chunks"] >= result.counters["chunks_received"]
    assert result.counters["server_chunk_ms_median"] > 0
    # The warm-up's inference ran before the episode, and its observation is not counted.
    log = (tmp_path / "policy_server.log").read_text()
    assert log.count("Running inference for observation") > result.counters["chunks_received"]


def test_lerobots_async_inference_stops_what_it_started_when_it_cannot_start(
    synthetic: Path, tmp_path: Path
) -> None:
    import socket

    from neura_robots.aloha.world import RealTimeWorld
    from neura_robots.evidence.lerobot_async import AsyncRun

    world = RealTimeWorld(AlohaSim()).start()
    try:
        run = AsyncRun(synthetic, world, workdir=tmp_path)
        with socket.socket() as taken:  # another program has the proxy's port
            taken.bind(("127.0.0.1", run.proxy.port))
            taken.listen()
            with pytest.raises(OSError, match="address already in use"), run:
                pass
    finally:
        world.close()
    server = run.server.process
    assert server is not None
    assert server.poll() is not None, "the policy server outlived the error"
    assert not run.proxy.thread.is_alive()


def test_the_site_is_given_absolute_paths(
    synthetic: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway runs in the site's directory: a checkpoint given relative to the caller's
    (as the evidence workflow does) must still be found."""
    import shutil

    import yaml

    from neura_robots.qualify.site import LocalSite

    shutil.copytree(synthetic, tmp_path / "checkpoint")
    deployment = write(tmp_path / "contracts", synthetic, name="act-aloha", source="synthetic")
    monkeypatch.chdir(tmp_path)
    site = LocalSite(
        Path("contracts/artifact.json"),
        Path("contracts/robot.json"),
        Path("contracts/profile.json"),
        Path("checkpoint"),
        "cpu",
        Path("site"),
    )
    config = yaml.safe_load((tmp_path / "site" / "site.yaml").read_text())
    (served,) = config["deployments"]
    for key in ("artifact", "robot_profile", "execution_profile", "checkpoint"):
        assert Path(served[key]).is_absolute(), key
        assert Path(served[key]).exists(), key
    assert site.workdir.is_absolute()
    assert deployment.checkpoint == synthetic


def test_lerobot_evals_outcomes_are_read_seed_by_seed() -> None:
    from neura_robots.evidence.cli import reference_agreement, reference_outcomes

    # LeRobot 0.6's eval_info.json: each task's successes in episode order.
    current = {
        "per_task": [{"task_group": "aloha", "metrics": {"successes": [False, True, True]}}],
        "overall": {"pc_success": 66.7, "n_episodes": 3},
    }
    assert reference_outcomes(current, 1000) == {1000: False, 1001: True, 1002: True}
    earlier = {"per_episode": [{"seed": 7, "success": True}]}
    assert reference_outcomes(earlier, 1000) == {7: True}
    ours = [{"seed": 1000, "success": False}, {"seed": 1001, "success": False}]
    agreement = reference_agreement(current, ours, 1000)
    assert agreement["episodes_compared"] == 2
    assert agreement["same_outcome"] == 1
    assert agreement["different_outcome_seeds"] == [1001]
    assert agreement["lerobot_eval_pc_success_reported"] == 66.7


def test_parity_with_lerobot_alone(synthetic: Path, tmp_path: Path) -> None:
    import argparse

    from neura_robots.evidence.cli import Context, parity
    from neura_robots.evidence.served import ServedRun

    deployment = write(tmp_path / "contracts", synthetic, name="act-aloha", source="synthetic")
    run = ServedRun(deployment, workdir=tmp_path / "site", parity_samples=3)
    asyncio.run(run.episodes([1000], max_steps=150))
    ctx = Context(argparse.Namespace(device="cpu"), tmp_path, synthetic, deployment, {})
    measured = parity(ctx, run.pairs)
    assert measured["samples"] == 3
    assert measured["max_abs_difference"] < 1e-4
