"""The ALOHA tasks on plain MuJoCo, against values recorded from gym-aloha 0.1.5 with dm_control
1.0.41 and MuJoCo 3.8.1 (neura_robots.aloha.fidelity, seed 1000, 400 steps)."""

from __future__ import annotations

import os

import numpy as np
import pytest

os.environ.setdefault("MUJOCO_GL", "egl")  # before MuJoCo is imported: headless rendering
mujoco = pytest.importorskip("mujoco")

from neura_robots.aloha import (  # noqa: E402
    JOINTS,
    MAX_REWARD,
    AlohaSim,
    normalize_gripper,
    sample_box_pose,
    unnormalize_gripper,
)
from neura_robots.aloha.fidelity import scripted_actions  # noqa: E402

# gym-aloha's qpos (arms, then the objects) after the scripted actions, at step 399.
REFERENCE_399 = {
    "transfer_cube": [
        -0.16650335699744293, -0.9414665086460899, 0.8440187749322385, 0.06491907281408493,
        -0.21210187572977934, -0.09572951203681686, 0.05681818843968708, -0.057047324013280526,
        0.12215202742144689, -1.0369202411223493, 1.3916142981761646, 0.10674166601224144,
        -0.3924795043513474, 0.11803144786393524, 0.05602928158543437, -0.056264553876370395,
        0.1307179170929219, 0.4230013886248812, 0.019999964686610753, 1.0,
    ],
    "insertion": [
        -0.16650335699744293, -0.9414665086460899, 0.8440187749322385, 0.06491907281408493,
        -0.21210187572977934, -0.09572951203681686, 0.05681818843968708, -0.057047324013280526,
        0.12215202742144689, -1.0369202411223493, 1.3916142981761646, 0.10674166601224144,
        -0.3924795043513474, 0.11803144786393524, 0.05602928158543437, -0.056264553876370395,
        0.16535895854646096, 0.4230013886248812, 0.00999996468661076, 1.0,
    ],
}  # fmt: skip


@pytest.mark.skipif(mujoco.__version__ != "3.8.1", reason="the reference was recorded on 3.8.1")
@pytest.mark.parametrize("task", ["transfer_cube", "insertion"])
def test_matches_gym_aloha(task: str) -> None:
    sim = AlohaSim(task)  # type: ignore[arg-type]
    sim.reset(1000)
    for action in scripted_actions(400, 1000):
        sim.step(action)
    reference = np.array(REFERENCE_399[task])
    np.testing.assert_allclose(sim.data.qpos[: len(reference)], reference, rtol=0, atol=1e-12)


def test_reset_is_seeded() -> None:
    sim = AlohaSim()
    sim.reset(7)
    first = sim.data.qpos.copy()
    sim.step(sim.qpos())
    sim.reset(7)
    np.testing.assert_array_equal(sim.data.qpos, first)
    np.testing.assert_array_equal(sim.data.qpos[16:23], sample_box_pose(7))
    assert sim.steps == 0
    assert sim.reward == 0


def test_observation_and_targets() -> None:
    sim = AlohaSim()
    sim.reset(1000)
    assert len(JOINTS) == 14 == sim.qpos().shape[0] == sim.qvel().shape[0]
    action = sim.qpos()
    action[6] = action[13] = 1.0  # open both grippers
    sim.step(action)
    np.testing.assert_allclose(sim.targets()[[6, 13]], [1.0, 1.0])
    assert normalize_gripper(unnormalize_gripper(0.3)) == pytest.approx(0.3)
    with pytest.raises(ValueError, match="14 values"):
        sim.step(np.zeros(13))


def test_hold_keeps_targets() -> None:
    sim = AlohaSim()
    sim.reset(1000)
    sim.step(sim.qpos())
    targets = sim.targets()
    for _ in range(10):
        sim.hold_step()
    np.testing.assert_array_equal(sim.targets(), targets)
    assert sim.steps == 11


@pytest.mark.parametrize(
    ("pairs", "reward"),
    [
        (set(), 0),
        ({("red_box", "vx300s_right/10_right_gripper_finger"), ("red_box", "table")}, 1),
        ({("red_box", "vx300s_right/10_right_gripper_finger")}, 2),
        ({("red_box", "vx300s_left/10_left_gripper_finger"), ("red_box", "table")}, 3),
        ({("red_box", "vx300s_left/10_left_gripper_finger")}, MAX_REWARD),
        # gym-aloha matches ordered pairs: the box must be the first geom of the contact
        ({("vx300s_left/10_left_gripper_finger", "red_box")}, 0),
    ],
)
def test_transfer_cube_reward(
    pairs: set[tuple[str, str]], reward: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = AlohaSim()
    monkeypatch.setattr(sim, "_contacts", lambda: pairs)
    assert sim._reward() == reward


@pytest.mark.parametrize(
    ("pairs", "reward"),
    [
        (
            {
                ("red_peg", "vx300s_right/10_right_gripper_finger"),
                ("socket-2", "vx300s_left/10_left_gripper_finger"),
                ("red_peg", "table"),
            },
            1,
        ),
        (
            {
                ("red_peg", "vx300s_right/10_right_gripper_finger"),
                ("socket-2", "vx300s_left/10_left_gripper_finger"),
            },
            2,
        ),
        ({("red_peg", "socket-3")}, 3),
        ({("red_peg", "pin"), ("red_peg", "table")}, MAX_REWARD),
    ],
)
def test_insertion_reward(
    pairs: set[tuple[str, str]], reward: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = AlohaSim("insertion")
    monkeypatch.setattr(sim, "_contacts", lambda: pairs)
    assert sim._reward() == reward


def test_success_is_any_step() -> None:
    sim = AlohaSim()
    sim.reset(1000)
    sim.max_reward_seen = MAX_REWARD
    sim.hold_step()  # reward drops back to 0 here
    assert sim.reward == 0
    assert sim.success


def test_renders_the_top_camera() -> None:
    sim = AlohaSim()
    sim.reset(1000)
    try:
        image = sim.render("top")
    except Exception as exc:  # no OpenGL on this machine
        pytest.skip(f"no offscreen rendering: {exc}")
    finally:
        sim.close()
    assert image.shape == (480, 640, 3)
    assert image.dtype == np.uint8
    assert image.std() > 10  # a scene, not a blank buffer
