"""The ALOHA bimanual tasks of gym-aloha (transfer cube, insertion) on the MuJoCo bindings alone.

gym-aloha (Apache-2.0, the scene files in ``assets`` are its own) is what LeRobot evaluates its
published ALOHA policies on. It needs dm_control, whose labmaze dependency cannot be installed on
Python 3.13; dm_control's part is small (stepping, resetting, rendering), so this module does the
same with ``mujoco`` directly. ``tests/test_aloha.py`` and ``neura-evidence fidelity`` check it
step for step against gym-aloha itself.

What it reproduces, from gym-aloha 0.1.5 and dm_control 1.0:

- ``reset(seed)``: reset the data, put both arms at ``START_ARM_POSE``, the box (or peg and
  socket) at gym-aloha's seeded pose, then a forward pass with actuation disabled
  (``Physics.reset_context``);
- ``step(action)``: the 14 commands (6 joints and a normalized gripper per arm) become the 16
  position targets, then dm_control's "legacy step" for one control period of 20 ms (``mj_step2``,
  ``mj_step`` for the remaining substeps, ``mj_step1``), so contacts are current when the reward
  reads them;
- the reward from the same contact pairs (4 is success);
- the observation: joint positions with normalized grippers, and the ``top`` camera at 640x480.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

# MuJoCo picks its OpenGL backend when it is imported: headless machines render with EGL (or
# OSMesa, if MUJOCO_GL says so).
os.environ.setdefault("MUJOCO_GL", "egl")

import mujoco

ASSETS = Path(__file__).parent / "assets"
DT = 0.02  # one control period, 50 Hz
FPS = 50

Task = Literal["transfer_cube", "insertion"]

JOINTS = [
    f"{side}_arm_{joint}"
    for side in ("left", "right")
    for joint in (
        "waist",
        "shoulder",
        "elbow",
        "forearm_roll",
        "wrist_angle",
        "wrist_rotate",
        "gripper",
    )
]

START_ARM_POSE = np.array(
    [0, -0.96, 1.16, 0, -0.3, 0, 0.02239, -0.02239] * 2,
    dtype=np.float64,
)

PUPPET_GRIPPER_POSITION_OPEN = 0.05800
PUPPET_GRIPPER_POSITION_CLOSE = 0.01844
_GRIPPER_SPAN = PUPPET_GRIPPER_POSITION_OPEN - PUPPET_GRIPPER_POSITION_CLOSE

MAX_REWARD = 4


def normalize_gripper(position: Any) -> Any:
    return (position - PUPPET_GRIPPER_POSITION_CLOSE) / _GRIPPER_SPAN


def unnormalize_gripper(value: Any) -> Any:
    return value * _GRIPPER_SPAN + PUPPET_GRIPPER_POSITION_CLOSE


def sample_box_pose(seed: int | None) -> NDArray[np.float64]:
    """gym_aloha.utils.sample_box_pose."""
    rng = np.random.RandomState(seed)
    ranges = np.vstack([[0.0, 0.2], [0.4, 0.6], [0.05, 0.05]])
    position = rng.uniform(ranges[:, 0], ranges[:, 1])
    return np.concatenate([position, [1.0, 0.0, 0.0, 0.0]])


def sample_insertion_pose(seed: int | None) -> NDArray[np.float64]:
    """gym_aloha.utils.sample_insertion_pose: the peg's pose, then the socket's."""
    rng = np.random.RandomState(seed)
    peg = rng.uniform([0.1, 0.4, 0.05], [0.2, 0.6, 0.05])
    socket = rng.uniform([-0.2, 0.4, 0.05], [-0.1, 0.6, 0.05])
    quat = [1.0, 0.0, 0.0, 0.0]
    return np.concatenate([peg, quat, socket, quat])


@dataclass(frozen=True, slots=True)
class Step:
    reward: int
    success: bool


class AlohaSim:
    """One ALOHA scene. Not thread-safe: one thread steps, captures and renders."""

    def __init__(
        self, task: Task = "transfer_cube", *, width: int = 640, height: int = 480
    ) -> None:
        if task not in ("transfer_cube", "insertion"):
            raise ValueError(f"unknown ALOHA task {task!r}")
        self.task: Task = task
        self.width = width
        self.height = height
        self.model = mujoco.MjModel.from_xml_path(str(ASSETS / f"bimanual_viperx_{task}.xml"))
        self.data = mujoco.MjData(self.model)
        self.n_sub_steps = round(DT / self.model.opt.timestep)
        if abs(self.n_sub_steps * self.model.opt.timestep - DT) > 1e-8:
            raise ValueError("the control period is not a whole number of physics steps")
        # One renderer per thread: its OpenGL context can be current on one thread only (an
        # EGL context used from another fails with EGL_BAD_ACCESS).
        self._renderers: dict[int, mujoco.Renderer] = {}
        self.reward = 0
        self.max_reward_seen = 0
        self.steps = 0
        self.seed: int | None = None
        names = [
            mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_GEOM, i) or ""
            for i in range(self.model.ngeom)
        ]
        self._geom_names = names

    # --- episode -------------------------------------------------------------------------------

    def reset(self, seed: int | None = None) -> None:
        self.seed = seed
        pose = (
            sample_box_pose(seed) if self.task == "transfer_cube" else sample_insertion_pose(seed)
        )
        mujoco.mj_resetData(self.model, self.data)
        self.data.qpos[:16] = START_ARM_POSE
        self.data.ctrl[:] = START_ARM_POSE
        self.data.qpos[-len(pose) :] = pose
        self._forward_without_actuation()
        self.reward = self._reward()
        self.max_reward_seen = self.reward
        self.steps = 0

    def step(self, action: NDArray[np.floating[Any]]) -> Step:
        """Apply one command vector (14) and advance one control period."""
        self.set_targets(action)
        return self.advance()

    def set_targets(self, action: NDArray[np.floating[Any]]) -> None:
        """The 14 commands (6 joints and a normalized gripper per arm) as the 16 position
        targets, without advancing time. The arithmetic is gym-aloha's, in the action's own
        precision (a policy's float32 actions stay float32 until they reach the actuators)."""
        action = np.asarray(action)
        if action.shape != (14,):
            raise ValueError(f"an ALOHA action has 14 values, got {action.shape}")
        if not np.issubdtype(action.dtype, np.floating):
            action = action.astype(np.float64)
        left = unnormalize_gripper(action[6])
        right = unnormalize_gripper(action[13])
        self.data.ctrl[:] = np.concatenate(
            [action[:6], [left, -left], action[7:13], [right, -right]]
        )

    def advance(self) -> Step:
        """Advance one control period with the current position targets: servos keep tracking
        their last goal whether or not a new command arrived."""
        self._legacy_step()
        self.steps += 1
        self.reward = self._reward()
        self.max_reward_seen = max(self.max_reward_seen, self.reward)
        return Step(self.reward, self.reward == MAX_REWARD)

    hold_step = advance

    @property
    def success(self) -> bool:
        """Whether the task succeeded at any point of the episode (LeRobot's ``is_success``)."""
        return self.max_reward_seen == MAX_REWARD

    # --- observation ---------------------------------------------------------------------------

    def qpos(self) -> NDArray[np.float64]:
        raw: NDArray[np.float64] = self.data.qpos
        return np.concatenate(
            [raw[:6], [normalize_gripper(raw[6])], raw[8:14], [normalize_gripper(raw[14])]]
        ).astype(np.float64)

    def qvel(self) -> NDArray[np.float64]:
        raw: NDArray[np.float64] = self.data.qvel
        return np.concatenate(
            [raw[:6], [raw[6] / _GRIPPER_SPAN], raw[8:14], [raw[14] / _GRIPPER_SPAN]]
        ).astype(np.float64)

    def targets(self) -> NDArray[np.float64]:
        """The current position targets as a command vector (14)."""
        ctrl: NDArray[np.float64] = self.data.ctrl
        return np.concatenate(
            [ctrl[:6], [normalize_gripper(ctrl[6])], ctrl[8:14], [normalize_gripper(ctrl[14])]]
        ).astype(np.float64)

    def render(
        self, camera: str = "top", *, data: mujoco.MjData | None = None
    ) -> NDArray[np.uint8]:
        """The camera's view of the scene, or of ``data`` (a copy of its state), drawn with the
        calling thread's own renderer."""
        renderer = self._renderers.get(threading.get_ident())
        if renderer is None:
            renderer = mujoco.Renderer(self.model, self.height, self.width)
            self._renderers[threading.get_ident()] = renderer
        renderer.update_scene(self.data if data is None else data, camera=camera)
        image: NDArray[np.uint8] = renderer.render()
        return image

    def release_renderer(self) -> None:
        """Frees the calling thread's renderer: a thread that draws calls it before it ends."""
        renderer = self._renderers.pop(threading.get_ident(), None)
        if renderer is not None:
            renderer.close()

    def close(self) -> None:
        """Frees the calling thread's renderer; another thread's is freed when it ends (its
        context cannot be made current here)."""
        self.release_renderer()
        self._renderers.clear()

    # --- dm_control's semantics ----------------------------------------------------------------

    def _forward_without_actuation(self) -> None:
        flags = self.model.opt.disableflags
        self.model.opt.disableflags = flags | mujoco.mjtDisableBit.mjDSBL_ACTUATION
        try:
            mujoco.mj_forward(self.model, self.data)
        finally:
            self.model.opt.disableflags = flags

    def _legacy_step(self) -> None:
        if self.model.opt.integrator != mujoco.mjtIntegrator.mjINT_RK4:
            mujoco.mj_step2(self.model, self.data)
            if self.n_sub_steps > 1:
                mujoco.mj_step(self.model, self.data, self.n_sub_steps - 1)
        else:
            mujoco.mj_step(self.model, self.data, self.n_sub_steps)
        mujoco.mj_step1(self.model, self.data)

    def _contacts(self) -> set[tuple[str, str]]:
        names = self._geom_names
        return {
            (names[self.data.contact[i].geom1], names[self.data.contact[i].geom2])
            for i in range(self.data.ncon)
        }

    def _reward(self) -> int:
        pairs = self._contacts()
        if self.task == "transfer_cube":
            touch_left = ("red_box", "vx300s_left/10_left_gripper_finger") in pairs
            touch_right = ("red_box", "vx300s_right/10_right_gripper_finger") in pairs
            touch_table = ("red_box", "table") in pairs
            reward = 0
            if touch_right:
                reward = 1
            if touch_right and not touch_table:  # lifted
                reward = 2
            if touch_left:  # attempted transfer
                reward = 3
            if touch_left and not touch_table:  # transferred
                reward = 4
            return reward
        sockets = [f"socket-{i}" for i in range(1, 5)]
        touch_right = ("red_peg", "vx300s_right/10_right_gripper_finger") in pairs
        touch_left = any((s, "vx300s_left/10_left_gripper_finger") in pairs for s in sockets)
        peg_table = ("red_peg", "table") in pairs
        socket_table = any((s, "table") in pairs for s in sockets)
        peg_socket = any(("red_peg", s) in pairs for s in sockets)
        pin = ("red_peg", "pin") in pairs
        reward = 0
        if touch_left and touch_right:
            reward = 1
        if touch_left and touch_right and not peg_table and not socket_table:
            reward = 2
        if peg_socket and not peg_table and not socket_table:
            reward = 3
        if pin:
            reward = 4
        return reward
