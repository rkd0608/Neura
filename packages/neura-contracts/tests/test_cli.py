from __future__ import annotations

import json
from pathlib import Path

import pytest

from neura_contracts.cli import main

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"
ARTIFACT = str(EXAMPLES / "act-so101.artifact.yaml")
ROBOT = str(EXAMPLES / "so101.robot.yaml")
PROFILE = str(EXAMPLES / "act-so101-30hz.profile.yaml")


def test_validate_and_digest(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", ARTIFACT, ROBOT]) == 0
    assert main(["digest", ROBOT]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1].startswith("sha256:")


def test_check_compatible_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["check", "--artifact", ARTIFACT, "--robot", ROBOT, "--profile", PROFILE, "--json"])
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["compatible"] is True
    assert report["issues"] == []


def test_check_wrong_kind_is_usage_error() -> None:
    assert main(["check", "--artifact", ROBOT, "--robot", ROBOT, "--profile", PROFILE]) == 2


def test_check_incompatible(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    robot = tmp_path / "robot.yaml"
    robot.write_text(
        Path(ROBOT).read_text().replace("control_period_us: 33333", "control_period_us: 20000")
    )
    assert main(["check", "--artifact", ARTIFACT, "--robot", str(robot), "--profile", PROFILE]) == 1
    out = capsys.readouterr().out
    assert "COMPAT_CONTROL_PERIOD_MISMATCH" in out
    assert "COMPAT_ROBOT_PROFILE_DIGEST_MISMATCH" in out


def test_invalid_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("api_version: neura.dev/v1alpha1\nkind: RobotProfile\nmetadata: {}\n")
    assert main(["validate", str(bad)]) == 1
    assert "CONTRACT_INVALID" in capsys.readouterr().err


def test_missing_file() -> None:
    assert main(["digest", "/nonexistent/file.yaml"]) == 2
