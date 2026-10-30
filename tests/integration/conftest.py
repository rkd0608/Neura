"""What the integration tests that run containers share: Docker, and the base images
deploy/docker/Dockerfile pins, so a test runs the same Python or nginx the released images do.

Images are pulled by digest from mirror.gcr.io, which production pulls from too (deploy/gcp): the
same bytes as Docker Hub's, without its rate limits. A test that needs one is skipped when Docker
is not running or the image cannot be pulled, and says which.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[2] / "deploy" / "docker" / "Dockerfile"


def pinned_image(stage: str) -> str:
    """The image `stage` of deploy/docker/Dockerfile is built FROM, with its digest."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    args = dict(re.findall(r"^ARG (\w+)=(\S+)$", text, re.M))
    stages = {name: image for image, name in re.findall(r"^FROM (\S+) AS (\S+)$", text, re.M)}
    image = stages[stage]
    while image in stages:  # FROM another stage: its base
        image = stages[image]
    return re.sub(r"\$\{(\w+)\}", lambda arg: args[arg.group(1)], image)


def mirrored(image: str) -> str:
    """A Docker Hub image by digest, as mirror.gcr.io serves it."""
    name, digest = image.split("@")
    repository = name.split(":")[0]
    if "/" not in repository:
        repository = f"library/{repository}"
    return f"mirror.gcr.io/{repository}@{digest}"


@pytest.fixture(scope="session")
def base_image() -> Callable[[str], str]:
    """Pull the base image of a Dockerfile stage (once) and return its reference."""
    if shutil.which("docker") is None:
        pytest.skip("needs Docker")
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0:
        pytest.skip("needs Docker running (docker info fails)")

    def get(stage: str) -> str:
        image = mirrored(pinned_image(stage))
        present = ["docker", "image", "inspect", image]
        if subprocess.run(present, capture_output=True, check=False).returncode != 0:
            pulled = subprocess.run(
                ["docker", "pull", "--quiet", image],
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
            )
            if pulled.returncode != 0:
                pytest.skip(f"cannot pull {image}: {pulled.stderr.strip()}")
        return image

    return get
