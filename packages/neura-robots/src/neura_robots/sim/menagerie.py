"""Robot arms from MuJoCo Menagerie (Google DeepMind's collection of accurate robot models) as
simulated robots: the Franka Panda, Universal Robots UR5e, Kinova Gen3, UFACTORY xArm7 and the
SO-ARM100 (the SO-101's predecessor), each with its own scene and position actuators.

The models are not copied into this repository: ``fetch`` downloads the five from Menagerie's git
repository at a pinned commit (about 85 MB, once, into ``~/.cache/neura-robot``), or uses a
checkout named by ``NEURA_MENAGERIE``. Each model keeps its own licence (Apache-2.0 or BSD-3; see
its directory).

A Menagerie arm's commands are its actuators, in the model's order, read from the model:

- an actuator on one hinge or slide joint is that joint's absolute position, in rad or m, in its
  control range (or the joint's range, or one turn either way for a continuous joint);
- any other actuator (the Panda's and the xArm's tendon-driven grippers) is a fraction of its
  control range, 0 at the bottom, 1 at the top.

Only position-like actuators are accepted (a fixed gain and an affine bias), so every command has
a position that holds it: what the arm reads back is the command that would hold the arm where it
is. A ``front`` camera is added, looking at the arm.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mujoco
import numpy as np
from numpy.typing import NDArray

from neura_robots.sim.scene import Command, Scene, robot_profile

REPOSITORY = "https://github.com/google-deepmind/mujoco_menagerie"
COMMIT = "b0655057a9df2abe39ea7fc989914ba6f248301c"
DT = 0.02  # one control period, 50 Hz
TURN = 2 * np.pi


@dataclass(frozen=True, slots=True)
class Model:
    directory: str  # in Menagerie
    title: str


MODELS = {
    "franka_panda": Model("franka_emika_panda", "Franka Emika Panda with its hand"),
    "ur5e": Model("universal_robots_ur5e", "Universal Robots UR5e"),
    "kinova_gen3": Model("kinova_gen3", "Kinova Gen3 (7 DoF)"),
    "xarm7": Model("ufactory_xarm7", "UFACTORY xArm7 with its gripper"),
    "so_arm100": Model("trs_so_arm100", "SO-ARM100 (TheRobotStudio)"),
}


def fetch(cache: Path | None = None) -> Path:
    """Menagerie's checkout holding the models: ``NEURA_MENAGERIE`` if set, else the cache,
    fetched at ``COMMIT`` the first time (with git, the five model directories only)."""
    configured = os.environ.get("NEURA_MENAGERIE")
    if configured:
        root = Path(configured).expanduser()
        missing = [m.directory for m in MODELS.values() if not (root / m.directory).is_dir()]
        if missing:
            raise FileNotFoundError(f"NEURA_MENAGERIE={root} has no {', '.join(missing)}")
        return root
    base = cache or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "neura-robot"
    root = base / "menagerie" / COMMIT
    if (root / ".complete").exists():
        return root
    if root.exists():
        shutil.rmtree(root)  # an earlier fetch that did not finish
    root.mkdir(parents=True)

    executable = shutil.which("git")
    if executable is None:
        raise FileNotFoundError("fetching the Menagerie models needs git")

    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603 - fixed arguments
            [executable, *args], cwd=root, check=True, capture_output=True, timeout=600
        )

    git("init", "-q")
    git("remote", "add", "origin", REPOSITORY)
    git("sparse-checkout", "set", "--no-cone", *(f"/{m.directory}/" for m in MODELS.values()))
    git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", COMMIT)
    git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")
    (root / ".complete").touch()
    return root


class MenagerieScene(Scene):
    cameras = ("front",)
    dt = DT

    def __init__(
        self, name: str, *, root: Path | None = None, width: int = 320, height: int = 240
    ) -> None:
        if name not in MODELS:
            raise ValueError(f"unknown Menagerie arm {name!r}: one of {', '.join(MODELS)}")
        self.name = name
        self.info = MODELS[name]
        path = (root or fetch()) / self.info.directory / "scene.xml"
        super().__init__(_with_front_camera(path, width, height), width=width, height=height)
        self.n_sub_steps = round(DT / self.model.opt.timestep)
        if abs(self.n_sub_steps * self.model.opt.timestep - DT) > 1e-8:
            raise ValueError(f"{name}: the control period is not a whole number of physics steps")
        self.commands = [_command(self.model, a) for a in range(self.model.nu)]
        self._home = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_KEY, "home")
        self.reset()

    def reset(self, seed: int | None = None) -> None:
        """The model's ``home`` keyframe (every arm here has one), holding there."""
        if self._home >= 0:
            mujoco.mj_resetDataKeyframe(self.model, self.data, self._home)
        else:
            mujoco.mj_resetData(self.model, self.data)
        mujoco.mj_forward(self.model, self.data)
        self.data.ctrl[:] = self._holding_ctrl()
        self.steps = 0

    def set_targets(self, action: NDArray[np.floating[Any]]) -> None:
        action = np.asarray(action, dtype=np.float64)
        if action.shape != (self.model.nu,):
            raise ValueError(f"{self.name} takes {self.model.nu} commands, got {action.shape}")
        ctrl = action.copy()
        for index, command in enumerate(self.commands):
            if command.unit == "fraction":
                low, high = self.model.actuator_ctrlrange[index]
                ctrl[index] = low + action[index] * (high - low)
        self.data.ctrl[:] = ctrl

    def advance(self) -> None:
        mujoco.mj_step(self.model, self.data, self.n_sub_steps)
        self.steps += 1

    def qpos(self) -> NDArray[np.float64]:
        """Where the arm is, as commands: the command that would hold it there."""
        return self._as_commands(self._holding_ctrl())

    def targets(self) -> NDArray[np.float64]:
        return self._as_commands(np.array(self.data.ctrl, dtype=np.float64))

    def profile(self) -> dict[str, Any]:
        return robot_profile(
            self,
            name=f"{self.name.replace('_', '-')}-sim",
            description=f"{self.info.title} in MuJoCo (MuJoCo Menagerie {COMMIT[:8]}), "
            "position actuators at 50 Hz, a front camera.",
            calibration=f"menagerie-{COMMIT[:8]}",
        )

    def _holding_ctrl(self) -> NDArray[np.float64]:
        # A position-like actuator's force is gain * ctrl + bias0 + bias1 * length (+ damping):
        # zero, at rest, for ctrl = -(bias0 + bias1 * length) / gain.
        gain = self.model.actuator_gainprm[:, 0]
        bias = self.model.actuator_biasprm
        ctrl = -(bias[:, 0] + bias[:, 1] * self.data.actuator_length) / gain
        return np.asarray(ctrl, dtype=np.float64)

    def _as_commands(self, ctrl: NDArray[np.float64]) -> NDArray[np.float64]:
        values: NDArray[np.float64] = ctrl.copy()
        for index, command in enumerate(self.commands):
            if command.unit == "fraction":
                low, high = self.model.actuator_ctrlrange[index]
                values[index] = (ctrl[index] - low) / (high - low)
        return values


def _command(model: mujoco.MjModel, index: int) -> Command:
    name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_ACTUATOR, index) or f"actuator{index}"
    position_like = (
        model.actuator_gaintype[index] == mujoco.mjtGain.mjGAIN_FIXED
        and model.actuator_biastype[index] == mujoco.mjtBias.mjBIAS_AFFINE
        and model.actuator_gainprm[index, 0] > 0
        and model.actuator_biasprm[index, 1] < 0
    )
    if not position_like:
        raise ValueError(f"actuator {name}: not a position actuator (fixed gain, affine bias)")
    limited = bool(model.actuator_ctrllimited[index])
    low, high = (float(v) for v in model.actuator_ctrlrange[index])
    joint = int(model.actuator_trnid[index, 0])
    on_joint = (
        model.actuator_trntype[index] == mujoco.mjtTrn.mjTRN_JOINT
        and model.jnt_type[joint] in (mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE)
        and model.actuator_gear[index, 0] == 1
        and model.actuator_gainprm[index, 0] == -model.actuator_biasprm[index, 1]
    )
    if on_joint:
        joint_name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, joint) or name
        unit = "rad" if model.jnt_type[joint] == mujoco.mjtJoint.mjJNT_HINGE else "m"
        if not limited and model.jnt_limited[joint]:
            low, high = (float(v) for v in model.jnt_range[joint])
        elif not limited:
            low, high = -TURN, TURN  # a continuous joint: one turn either way
        return Command(joint_name, unit, "absolute_joint_position", low, high)
    if not limited:
        raise ValueError(f"actuator {name}: a gripper needs a control range")
    return Command(name, "fraction", "gripper_position", 0.0, 1.0)


def _with_front_camera(path: Path, width: int, height: int) -> mujoco.MjModel:
    """The scene with a ``front`` camera looking at the arm from in front, above and aside, and
    an offscreen buffer the camera size."""
    spec = mujoco.MjSpec.from_file(str(path))
    model = spec.compile()
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    # The arm: the bodies under the first body attached to the world, at its home pose.
    root = next(b for b in range(1, model.nbody) if model.body_parentid[b] == 0)
    bodies = [b for b in range(1, model.nbody) if model.body_rootid[b] == root]
    if model.nkey:
        mujoco.mj_resetDataKeyframe(model, data, 0)
        mujoco.mj_forward(model, data)
    base = data.xpos[root]
    size = max(0.2, float(np.max(np.linalg.norm(data.xpos[bodies] - base, axis=1))))
    target = base + np.array([0.0, 0.0, 0.45 * size])
    position = target + size * np.array([1.7, -1.4, 0.9])
    forward = (target - position) / np.linalg.norm(target - position)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    rotation = np.column_stack([right, up, -forward])  # a camera looks along its -z axis
    quat = np.zeros(4)
    mujoco.mju_mat2Quat(quat, rotation.flatten())
    spec.worldbody.add_camera(name="front", pos=position, quat=quat, fovy=50)
    spec.visual.global_.offwidth = max(spec.visual.global_.offwidth, width)
    spec.visual.global_.offheight = max(spec.visual.global_.offheight, height)
    compiled: mujoco.MjModel = spec.compile()
    return compiled
