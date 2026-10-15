"""``neura-arm`` configuration and ``neura-arm check``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from neura_robots.cli import RobotConfig

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "robots" / "so101-arm.yaml"


def test_the_example_configuration_is_valid() -> None:
    config = RobotConfig.load(EXAMPLE)
    assert config.robot_id == "so101-a"
    assert config.revision is None  # runs what the site assigns
    assert config.arm.on_exit == "hold"
    assert set(config.cameras) == {"front", "wrist"}
    so101 = config.so101()
    assert so101.max_relative_target == 10


def test_credentials_are_a_certificate_or_a_token(tmp_path: Path) -> None:
    data = yaml.safe_load(EXAMPLE.read_text())
    data["credentials"] = {"cert_dir": "certs", "token": "t"}
    path = tmp_path / "arm.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError, match="cert_dir"):
        RobotConfig.load(path)


def test_relative_paths_are_relative_to_the_file(tmp_path: Path) -> None:
    data = yaml.safe_load(EXAMPLE.read_text())
    data["revision"] = {
        "id": "rev_local",
        "artifact": "contracts/a.yaml",
        "robot_profile": "contracts/r.yaml",
        "execution_profile": "contracts/p.yaml",
    }
    data["arm"]["calibration_dir"] = "calibration"
    path = tmp_path / "arm.yaml"
    path.write_text(yaml.safe_dump(data))
    config = RobotConfig.load(path)
    assert config.revision is not None
    assert config.revision.artifact == tmp_path / "contracts" / "a.yaml"
    assert config.arm.calibration_dir == tmp_path / "calibration"


def test_check_on_the_emulated_arm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("lerobot", reason="needs the models group: uv sync --group models")
    from neura_robots import cli
    from neura_robots.emulator import SO101Emulator, write_calibration

    with SO101Emulator(positions_deg={"wrist_flex": 12.0}) as emulator:
        write_calibration(emulator, tmp_path / "calibration", "arm-1")
        data = {
            "robot_id": "arm-1",
            "gateway": "127.0.0.1:1",
            "credentials": {"token": "dev"},
            "arm": {
                "port": emulator.port,
                "calibration_id": "arm-1",
                "calibration_dir": "calibration",
                "lock_dir": "locks",
            },
            "cameras": {"front": {"type": "synthetic"}, "wrist": {"type": "synthetic"}},
        }
        path = tmp_path / "arm.yaml"
        path.write_text(yaml.safe_dump(data))
        profile = ROOT / "examples" / "contracts" / "so101.robot.yaml"
        assert cli.main(["check", str(path), "--robot-profile", str(profile)]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["ready"] is True
        assert report["positions"]["wrist_flex.pos"] == pytest.approx(12.0, abs=0.5)
        assert report["cameras"]["front"] == {"width": 640, "height": 480}

        wide = yaml.safe_load(profile.read_text())
        wide["spec"]["command_interface"][0]["max"] = 170.0
        wide_path = tmp_path / "wide.robot.yaml"
        wide_path.write_text(yaml.safe_dump(wide))
        assert cli.main(["check", str(path), "--robot-profile", str(wide_path)]) == 1
        assert "calibrated range" in capsys.readouterr().err
