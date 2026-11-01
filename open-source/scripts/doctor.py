"""Check this machine for everything neura-robot can use, and say how to get what is missing.

    python scripts/doctor.py            # or: make doctor
    python scripts/doctor.py --offline  # skip the network checks

Only the core (Python 3.10+ and neura-robot installed) is required; everything else unlocks a
part of the repository: the simulated arms, the SO-101 emulator, the policy servers, the Docker
tests, Hugging Face models and datasets. Exits non-zero only when the core is missing.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

OK, NO, OPTIONAL = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[33m-\033[0m"
if not sys.stdout.isatty():
    OK, NO, OPTIONAL = "ok  ", "MISS", "opt "


def line(mark: str, what: str, detail: str = "") -> None:
    print(f"  {mark} {what}" + (f"\n        {detail}" if detail else ""))


def has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def run(*command: str) -> bool:
    try:
        done = subprocess.run(command, capture_output=True, timeout=20, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def reachable(url: str) -> bool:
    try:
        request = urllib.request.Request(url, method="HEAD")  # noqa: S310 - fixed https URLs
        with urllib.request.urlopen(request, timeout=8):  # noqa: S310
            return True
    except Exception:
        return False


def core() -> bool:
    print("Core")
    version = sys.version_info
    python_ok = version >= (3, 10)
    line(
        OK if python_ok else NO,
        f"Python {version.major}.{version.minor}",
        "" if python_ok else "neura-robot needs Python 3.10 or newer",
    )
    installed = has("neura_runtime") and has("neura_robots")
    line(
        OK if installed else NO,
        "neura-robot installed",
        "" if installed else 'pip install -e ".[dev,openpi]"   (make setup)',
    )
    line(
        OK if has("pytest") and has("ruff") else OPTIONAL,
        "development tools (pytest, ruff, mypy)",
        "" if has("pytest") else 'pip install -e ".[dev]"',
    )
    return python_ok and installed


def extras() -> None:
    print("Extras")
    lerobot_python = sys.version_info >= (3, 12)
    line(
        OK if has("websockets") and has("msgpack") else OPTIONAL,
        "openpi transport",
        "" if has("websockets") else 'pip install -e ".[openpi]"',
    )
    lerobot = has("lerobot")
    line(
        OK if lerobot else OPTIONAL,
        "LeRobot: SO-101 driver and emulator, LeRobot server",
        ""
        if lerobot
        else ("make setup-all" if lerobot_python else "needs Python 3.12+ (LeRobot's requirement)"),
    )
    if not has("mujoco"):
        line(OPTIONAL, "MuJoCo: simulated arms and ALOHA", 'pip install -e ".[sim]"')
        return
    try:
        import mujoco

        import neura_robots.sim  # noqa: F401 - picks MuJoCo's OpenGL backend

        model = mujoco.MjModel.from_xml_string("<mujoco><worldbody/></mujoco>")
        with mujoco.Renderer(model, 8, 8) as renderer:
            renderer.render()
        line(
            OK, f"MuJoCo {mujoco.__version__}, rendering with {os.environ.get('MUJOCO_GL', 'glfw')}"
        )
    except Exception as error:
        line(
            NO,
            "MuJoCo cannot render offscreen",
            f"{error}\n        Debian/Ubuntu: sudo apt-get "
            "install libegl1 libgl1 (or MUJOCO_GL=osmesa with libosmesa6)",
        )
    git = shutil.which("git") is not None
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "neura-robot"
    fetched = any((cache / "menagerie").glob("*/.complete")) or bool(
        os.environ.get("NEURA_MENAGERIE")
    )
    line(
        OK if fetched else (OPTIONAL if git else NO),
        "MuJoCo Menagerie arm models",
        ""
        if fetched
        else ("fetched on first use; now: make assets" if git else "fetching them needs git"),
    )


def docker() -> None:
    print("Docker (openpi's server and ROS 2 Jazzy tests)")
    if shutil.which("docker") is None:
        line(
            OPTIONAL,
            "Docker",
            "install Docker Engine or Docker Desktop: https://docs.docker.com/get-docker/",
        )
        return
    if not run("docker", "info"):
        line(NO, "Docker daemon", "start Docker, and let your user run it (docker info)")
        return
    line(OK, "Docker daemon")
    for image, folder in (
        ("neura-openpi-test:dev", "packages/neura-runtime/tests/openpi"),
        ("neura-ros2-test:dev", "packages/neura-robots/tests/ros2"),
    ):
        built = run("docker", "image", "inspect", image)
        line(
            OK if built else OPTIONAL,
            image,
            "" if built else f"make test-docker builds it ({folder})",
        )


def hugging_face(offline: bool) -> None:
    print("Hugging Face (models and datasets)")
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    hub = has("huggingface_hub")
    line(
        OK if hub else OPTIONAL,
        "huggingface_hub",
        ""
        if hub
        else 'comes with LeRobot: pip install -e ".[lerobot]"  (or pip install huggingface_hub)',
    )
    token = bool(os.environ.get("HF_TOKEN")) or (home / "token").is_file()
    line(
        OK if token else OPTIONAL,
        "signed in (needed only for gated or private repositories)",
        "" if token else "hf auth login   (public models and datasets need no account)",
    )
    line(OK, f"cache: {home}", "set HF_HOME to keep models and datasets elsewhere")
    if not offline:
        ok = reachable(
            "https://huggingface.co/api/models/lerobot/act_aloha_sim_transfer_cube_human"
        )
        line(
            OK if ok else NO,
            "huggingface.co reachable",
            "" if ok else "a firewall or proxy blocks it: allow huggingface.co and *.hf.co",
        )


def network() -> None:
    print("Network")
    menagerie = run(
        "git", "ls-remote", "https://github.com/google-deepmind/mujoco_menagerie", "HEAD"
    )
    line(
        OK if menagerie else NO,
        "github.com over git (Menagerie models)",
        "" if menagerie else "blocked: allow github.com, or point NEURA_MENAGERIE at a checkout",
    )
    pypi = reachable("https://pypi.org/simple/")
    line(OK if pypi else NO, "pypi.org (packages)", "" if pypi else "blocked: allow pypi.org")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--offline", action="store_true", help="skip the network checks")
    args = parser.parse_args()
    ready = core()
    if ready:
        extras()
    docker()
    hugging_face(args.offline)
    if not args.offline:
        network()
    print("\nReady for the core: make check" if ready else "\nInstall the core first: make setup")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
