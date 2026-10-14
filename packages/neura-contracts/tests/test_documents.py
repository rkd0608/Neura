from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from neura_contracts import (
    ContractError,
    ExecutionProfile,
    ModelArtifact,
    ReasonCode,
    RobotProfile,
    dump_document,
    load_document,
    parse_document,
)

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.yaml")), ids=lambda p: p.name)
def test_examples_are_valid(path: Path) -> None:
    document = load_document(path.read_text(encoding="utf-8"))
    assert isinstance(document, ModelArtifact | RobotProfile | ExecutionProfile)


def test_unsupported_api_version(robot_data: dict[str, Any]) -> None:
    robot_data["api_version"] = "neura.dev/v2"
    with pytest.raises(ContractError) as exc:
        parse_document(robot_data)
    assert exc.value.code is ReasonCode.CONTRACT_UNSUPPORTED_API_VERSION


def test_unknown_kind(robot_data: dict[str, Any]) -> None:
    robot_data["kind"] = "Robot"
    with pytest.raises(ContractError) as exc:
        parse_document(robot_data)
    assert exc.value.code is ReasonCode.CONTRACT_INVALID


@pytest.mark.parametrize("text", ["[1, 2]", "key: [unclosed", ""])
def test_malformed_input(text: str) -> None:
    with pytest.raises(ContractError) as exc:
        load_document(text)
    assert exc.value.code is ReasonCode.CONTRACT_INVALID


def test_validation_errors_are_listed_with_locations(robot_data: dict[str, Any]) -> None:
    robot_data["spec"]["control_period_us"] = 0
    robot_data["spec"]["unexpected"] = True
    with pytest.raises(ContractError) as exc:
        parse_document(robot_data)
    assert exc.value.code is ReasonCode.CONTRACT_INVALID
    joined = "\n".join(exc.value.errors)
    assert "spec.control_period_us" in joined
    assert "spec.unexpected" in joined


def test_strict_integers_reject_strings_and_floats(robot_data: dict[str, Any]) -> None:
    for bad in ["33333", 33333.0, True]:
        data = copy.deepcopy(robot_data)
        data["spec"]["control_period_us"] = bad
        with pytest.raises(ContractError):
            parse_document(data)


def test_documents_are_immutable(robot_data: dict[str, Any]) -> None:
    robot = parse_document(robot_data)
    assert isinstance(robot, RobotProfile)
    with pytest.raises(ValidationError):
        robot.spec.control_period_us = 1  # type: ignore[misc]


def test_dump_round_trip_preserves_digest(artifact_data: dict[str, Any]) -> None:
    artifact = parse_document(artifact_data)
    again = load_document(dump_document(artifact))
    assert again == artifact
    assert again.digest() == artifact.digest()


def test_digest_ignores_key_order_and_unset_optionals(robot_data: dict[str, Any]) -> None:
    reordered = dict(reversed(list(robot_data.items())))
    assert parse_document(reordered).digest() == parse_document(robot_data).digest()


def test_digest_changes_with_content(robot_data: dict[str, Any]) -> None:
    original = parse_document(robot_data).digest()
    robot_data["spec"]["command_interface"][0]["max"] = 111.0
    assert parse_document(robot_data).digest() != original


def test_digest_matches_published_example() -> None:
    # Pinned so any change to canonicalization or digest rules is a deliberate, reviewed change.
    robot = load_document((EXAMPLES / "so101.robot.yaml").read_text(encoding="utf-8"))
    profile = load_document((EXAMPLES / "act-so101-30hz.profile.yaml").read_text(encoding="utf-8"))
    assert isinstance(profile, ExecutionProfile)
    assert profile.spec.binding.robot_profile == robot.digest()
