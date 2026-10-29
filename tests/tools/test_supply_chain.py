"""Every image the deployment pins is where Dependabot proposes its updates (design §17.7, supply
chain): in a Dockerfile or a compose file of a directory .github/dependabot.yml lists, never in a
script, which no updater reads."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PINNED = re.compile(rb"[\w./:-]+@sha256:[0-9a-f]{64}")
# The files Dependabot's docker and docker-compose ecosystems read.
ECOSYSTEMS = {
    "docker": re.compile(r"^Dockerfile"),
    "docker-compose": re.compile(r"(docker-)?compose(-\w+)?(\.[\w-]+)?\.ya?ml$"),
}


def problems(root: Path) -> list[str]:
    config = yaml.safe_load((root / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    watched = {(u["package-ecosystem"], u["directory"]) for u in config["updates"]}
    found = []
    for path in sorted((root / "deploy").rglob("*")):
        if not path.is_file() or not PINNED.search(path.read_bytes()):
            continue
        name = path.relative_to(root).as_posix()
        directory = "/" + path.parent.relative_to(root).as_posix()
        kinds = [kind for kind, files in ECOSYSTEMS.items() if files.search(path.name)]
        if not kinds:
            found.append(f"{name} pins an image Dependabot cannot see: move it to a compose file")
        elif not any((kind, directory) in watched for kind in kinds):
            found.append(f"{name}: .github/dependabot.yml does not watch {directory} ({kinds[0]})")
    return found


def test_every_pinned_image_is_one_dependabot_updates() -> None:
    assert problems(ROOT) == []
