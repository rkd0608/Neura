from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, parse_document

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"


def _load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture
def artifact_data() -> dict[str, Any]:
    return _load("act-so101.artifact.yaml")


@pytest.fixture
def language_artifact_data() -> dict[str, Any]:
    return _load("smolvla-so101.artifact.yaml")


@pytest.fixture
def robot_data() -> dict[str, Any]:
    return _load("so101.robot.yaml")


@pytest.fixture
def profile_data() -> dict[str, Any]:
    return _load("act-so101-30hz.profile.yaml")


@pytest.fixture
def language_profile_data() -> dict[str, Any]:
    return _load("smolvla-so101-30hz.profile.yaml")


Build = Callable[..., tuple[ModelArtifact, RobotProfile, ExecutionProfile]]


@pytest.fixture
def build() -> Build:
    """Parse (artifact, robot, profile) dicts, rebinding the profile to the parsed digests."""

    def _build(
        artifact: dict[str, Any],
        robot: dict[str, Any],
        profile: dict[str, Any],
        *,
        rebind: bool = True,
    ) -> tuple[ModelArtifact, RobotProfile, ExecutionProfile]:
        a = parse_document(copy.deepcopy(artifact))
        r = parse_document(copy.deepcopy(robot))
        assert isinstance(a, ModelArtifact)
        assert isinstance(r, RobotProfile)
        p_data = copy.deepcopy(profile)
        if rebind:
            p_data["spec"]["binding"] = {"model_artifact": a.digest(), "robot_profile": r.digest()}
        p = parse_document(p_data)
        assert isinstance(p, ExecutionProfile)
        return a, r, p

    return _build
