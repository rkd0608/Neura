"""What a simulated robot is to the rest of Neura: a MuJoCo scene with named commands, a control
period and cameras (``Scene``), and the robot profile that describes it (``robot_profile``).

A scene's command vector is what its robot profile commands, in order and units; ``set_targets``
makes it the actuators' targets without advancing time, ``advance`` runs one control period with
whatever targets are current (servos keep tracking their last goal), ``qpos`` and ``targets``
read the robot back in command units. ``SimArm`` and the worlds use nothing else.

Rendering is the same for every scene: one renderer per thread, since an OpenGL context can be
current on one thread only (an EGL context used from another fails with EGL_BAD_ACCESS).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import mujoco
import numpy as np
from numpy.typing import NDArray

API = "neura.dev/v1alpha1"


@dataclass(frozen=True, slots=True)
class Command:
    """One element of a scene's command vector, as its robot profile states it."""

    name: str
    unit: str  # a neura_contracts Unit
    semantics: str  # a neura_contracts ActionSemantics
    low: float
    high: float


class Scene:
    """One scene. Not thread-safe: one thread steps it (the worlds take a lock around it)."""

    commands: list[Command]
    cameras: tuple[str, ...]
    dt: float  # one control period, in seconds
    controller: str = "mujoco-position-actuators"

    def __init__(self, model: mujoco.MjModel, *, width: int, height: int) -> None:
        self.model = model
        self.data = mujoco.MjData(model)
        self.width = width
        self.height = height
        self.steps = 0
        self._renderers: dict[int, mujoco.Renderer] = {}

    @property
    def joints(self) -> list[str]:
        return [c.name for c in self.commands]

    @property
    def success(self) -> bool:
        """Whether the scene's task succeeded; a scene without a task never does."""
        return False

    # --- what each scene defines ---------------------------------------------------------------

    def reset(self, seed: int | None = None) -> None:
        raise NotImplementedError

    def set_targets(self, action: NDArray[np.floating[Any]]) -> None:
        raise NotImplementedError

    def advance(self) -> object:
        raise NotImplementedError

    def qpos(self) -> NDArray[np.float64]:
        raise NotImplementedError

    def targets(self) -> NDArray[np.float64]:
        raise NotImplementedError

    # --- rendering -----------------------------------------------------------------------------

    def render(self, camera: str, *, data: mujoco.MjData | None = None) -> NDArray[np.uint8]:
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


def robot_profile(
    scene: Scene, *, name: str, description: str, calibration: str, version: str = "r1"
) -> dict[str, Any]:
    """The robot profile of ``scene``: its commands (each also a state feature), its control
    period and cameras, hold and controlled stop as its fault responses."""
    commands = [
        {
            "name": c.name,
            "unit": c.unit,
            "frame": "joint_space",
            "semantics": c.semantics,
            "min": round(c.low, 5),
            "max": round(c.high, 5),
        }
        for c in scene.commands
    ]
    return {
        "api_version": API,
        "kind": "RobotProfile",
        "metadata": {"name": name, "version": version, "description": description},
        "spec": {
            "controller": {"name": scene.controller, "revision": mujoco.__version__},
            "control_period_us": round(scene.dt * 1_000_000),
            "command_interface": commands,
            "state_features": [
                {"name": c["name"], "unit": c["unit"], "frame": "joint_space"} for c in commands
            ],
            "cameras": [
                {
                    "role": role,
                    "width": scene.width,
                    "height": scene.height,
                    "color_order": "rgb",
                    "encodings": ["raw"],
                    "calibration_revision": calibration,
                }
                for role in scene.cameras
            ],
            "fault_responses": ["hold_position", "controlled_stop"],
        },
    }
