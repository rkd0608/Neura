"""Fixtures shared by every package's tests: the example contracts as live objects."""

from __future__ import annotations

import copy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

EXAMPLES = Path(__file__).resolve().parent / "examples" / "contracts"
# The agent's tests share the core harness in test_core (imported by test_robots).
sys.path.insert(0, str(Path(__file__).resolve().parent / "packages" / "neura-agent" / "tests"))


def example_data(name: str) -> dict[str, Any]:
    data = yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


BindingFactory = Callable[..., Any]


def make_binding_factory() -> BindingFactory:
    """Build an ExecutionBinding from the ACT (default) or SmolVLA example, with optional edits.

    ``edit(artifact, robot, profile)`` mutates the raw dicts before parsing; the profile is
    rebound to the edited documents' digests.
    """
    from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, parse_document
    from neura_runtime.binding import ExecutionBinding

    def factory(
        model: str = "act",
        edit: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], object] | None = None,
    ) -> ExecutionBinding:
        artifact = copy.deepcopy(example_data(f"{model}-so101.artifact.yaml"))
        robot = copy.deepcopy(example_data("so101.robot.yaml"))
        profile = copy.deepcopy(example_data(f"{model}-so101-30hz.profile.yaml"))
        if edit is not None:
            edit(artifact, robot, profile)
        a = parse_document(artifact)
        r = parse_document(robot)
        assert isinstance(a, ModelArtifact)
        assert isinstance(r, RobotProfile)
        profile["spec"]["binding"] = {"model_artifact": a.digest(), "robot_profile": r.digest()}
        p = parse_document(profile)
        assert isinstance(p, ExecutionProfile)
        return ExecutionBinding(a, r, p)

    return factory


@pytest.fixture
def make_binding() -> BindingFactory:
    """Build an ExecutionBinding from the ACT (default) or SmolVLA example, with optional edits
    (see make_binding_factory)."""
    return make_binding_factory()


@pytest.fixture(scope="session")
def binding_factory() -> BindingFactory:
    """make_binding for module- and session-scoped fixtures."""
    return make_binding_factory()


# --- raw example documents (contracts tests) ---------------------------------------------


@pytest.fixture
def artifact_data() -> dict[str, Any]:
    return example_data("act-so101.artifact.yaml")


@pytest.fixture
def language_artifact_data() -> dict[str, Any]:
    return example_data("smolvla-so101.artifact.yaml")


@pytest.fixture
def robot_data() -> dict[str, Any]:
    return example_data("so101.robot.yaml")


@pytest.fixture
def profile_data() -> dict[str, Any]:
    return example_data("act-so101-30hz.profile.yaml")


@pytest.fixture
def language_profile_data() -> dict[str, Any]:
    return example_data("smolvla-so101-30hz.profile.yaml")


@pytest.fixture
def build() -> Callable[..., Any]:
    """Parse (artifact, robot, profile) dicts, rebinding the profile to the parsed digests."""

    from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, parse_document

    def _build(
        artifact: dict[str, Any],
        robot: dict[str, Any],
        profile: dict[str, Any],
        *,
        rebind: bool = True,
    ) -> tuple[Any, Any, Any]:
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
