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
