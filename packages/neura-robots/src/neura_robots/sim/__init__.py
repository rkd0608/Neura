"""Simulated robots on MuJoCo: one scene interface (``scene.Scene``), its clock (``world``) and
the arm the runtime drives (``arm.SimArm``), shared by the simulated ALOHA (``neura_robots.aloha``)
and the MuJoCo Menagerie arms (``menagerie``).

MuJoCo picks its OpenGL backend when it is imported, so this package picks it first: EGL, for
headless rendering, when libEGL is installed and ``MUJOCO_GL`` does not say otherwise. Without
libEGL, importing MuJoCo with ``MUJOCO_GL=egl`` fails deep inside PyOpenGL; here it is an
``ImportError`` that says what to install (tests skip on it with ``pytest.importorskip``).
"""

from __future__ import annotations

import ctypes.util
import os

if "MUJOCO_GL" not in os.environ and ctypes.util.find_library("EGL"):
    os.environ["MUJOCO_GL"] = "egl"
if os.environ.get("MUJOCO_GL") == "egl" and not ctypes.util.find_library("EGL"):
    raise ImportError(
        "MUJOCO_GL=egl, but libEGL is not installed: install it (Debian or Ubuntu: "
        "apt-get install libegl1 libgl1), or set MUJOCO_GL=osmesa (with libosmesa6) or glfw"
    )
