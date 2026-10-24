"""The `neura` command's pieces that need no control plane: reading policies from their three
sources, signing keys, putting files where local sites find them, `neura check`, and the settings
`neura site join` gives a local agent (design §29)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from neura_cli import keys, sources, weights
from neura_cli.app import main
from neura_cli.config import Config
from neura_cli.local_site import LocalSite
from neura_cli.session import CliError
from neura_contracts import ModelArtifact, authoring, parse_document, signing

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"

HOLD = '''
import numpy as np

import neura


@neura.policy(chunk_length=10, cameras={"front": (64, 48)}, weights="weights")
class Hold:
    """Holds the arm where it is, plus an offset read from its weights."""

    def __init__(self, weights, device):
        self.offset = float((weights / "offset.txt").read_text())

    def act(self, observation):
        assert observation["observation.images.front"].shape == (48, 64, 3)
        return np.tile(observation["observation.state"] + self.offset, (10, 1))
'''


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEURA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for name in ("NEURA_TOKEN", "NEURA_API_URL", "NEURA_WORKSPACE", "NEURA_PUBLISHER_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def robot() -> dict[str, Any]:
    data = yaml.safe_load((EXAMPLES / "so101.robot.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def python_policy(directory: Path, source: str = HOLD) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hold.py").write_text(source, encoding="utf-8")
    (directory / "weights").mkdir(exist_ok=True)
    (directory / "weights" / "offset.txt").write_text("0.5", encoding="utf-8")
    (directory / "__pycache__").mkdir(exist_ok=True)
    (directory / "__pycache__" / "hold.cpython-313.pyc").write_bytes(b"cache")
    return directory / "hold.py"


def lerobot_checkpoint(directory: Path, **config: Any) -> Path:
    directory.mkdir(parents=True)
    body = {
        "type": "act",
        "chunk_size": 100,
        "n_action_steps": 100,
        "input_features": {
            "observation.state": {"type": "STATE", "shape": [6]},
            "observation.images.front": {"type": "VISUAL", "shape": [3, 480, 640]},
            "observation.images.wrist": {"type": "VISUAL", "shape": [3, 480, 640]},
        },
        "output_features": {"action": {"type": "ACTION", "shape": [6]}},
        **config,
    }
    (directory / "config.json").write_text(json.dumps(body), encoding="utf-8")
    (directory / "model.safetensors").write_bytes(os.urandom(4096))
    return directory


# --- sources -----------------------------------------------------------------------------------


def test_a_python_policy_ships_its_code_and_weights_with_how_to_run_it(
    tmp_path: Path, robot: dict[str, Any]
) -> None:
    path = python_policy(tmp_path / "hold")
    source = sources.load(str(path), robot)
    assert source.kind == "python"
    assert source.reference.endswith("hold.py:Hold")
    assert source.declaration.adapter == "python"
    assert source.declaration.description.startswith("Holds the arm")
    assert not source.declaration.batchable  # no act_batch
    files = {f.path: f for f in source.files}
    assert set(files) == {"hold.py", "weights/offset.txt", authoring.ENTRY_FILE}  # no caches
    entry = json.loads(files[authoring.ENTRY_FILE].source.read_text(encoding="utf-8"))
    assert entry["entry"] == "hold:Hold"
    assert entry["weights"] == "weights"
    assert entry["declaration"]["chunk_length"] == 10

    artifact = parse_document(source.artifact(robot, "hold"))
    assert isinstance(artifact, ModelArtifact)
    assert artifact.spec.adapter.name == "python"
    images = [i for i in artifact.spec.inputs if i.kind == "image"]
    assert [(i.camera_role, i.width, i.height) for i in images] == [("front", 64, 48)]
    # The same files are the same version; a change is a new one.
    assert sources.load(str(path), robot).version() == source.version()
    (tmp_path / "hold" / "weights" / "offset.txt").write_text("0.7", encoding="utf-8")
    assert sources.load(str(path), robot).version() != source.version()


def test_python_sources_name_the_class_when_a_file_has_several(
    tmp_path: Path, robot: dict[str, Any]
) -> None:
    two = HOLD + "\n\n@neura.policy(chunk_length=5)\nclass Other(Hold):\n    pass\n"
    path = python_policy(tmp_path / "two", two)
    with pytest.raises(CliError) as several:
        sources.load(str(path), robot)
    assert "Hold" in " ".join(several.value.details)
    assert sources.load(f"{path}:Other", robot).declaration.chunk_length == 5
    with pytest.raises(CliError, match=r"no @neura\.policy class Missing"):
        sources.load(f"{path}:Missing", robot)
    # A directory with one decorated file is that file.
    assert sources.load(str(python_policy(tmp_path / "one")).rsplit("/", 1)[0], robot).kind == (
        "python"
    )


def test_a_policy_that_does_not_fit_the_robot_says_why(
    tmp_path: Path, robot: dict[str, Any]
) -> None:
    wrong = HOLD.replace('cameras={"front": (64, 48)}', 'cameras={"top": (64, 48)}')
    path = python_policy(tmp_path / "wrong", wrong)
    with pytest.raises(CliError) as unfit:
        sources.load(str(path), robot).artifact(robot, "wrong")
    assert unfit.value.what == "This policy does not fit so101-follower."
    assert any("'top'" in d and "front, wrist" in d for d in unfit.value.details)


def test_a_lerobot_checkpoint_declares_itself(tmp_path: Path, robot: dict[str, Any]) -> None:
    checkpoint = lerobot_checkpoint(tmp_path / "act")
    (checkpoint / ".cache").mkdir()
    (checkpoint / ".cache" / "lock").write_text("x", encoding="utf-8")
    source = sources.load(str(checkpoint), robot)
    assert (source.kind, source.declaration.adapter) == ("lerobot", "lerobot-act")
    assert [f.path for f in source.files] == ["config.json", "model.safetensors"]
    artifact = source.artifact(robot, "pick")
    assert artifact["spec"]["outputs"]["chunk_length"] == 100

    with pytest.raises(CliError) as unknown:
        sources.load(str(lerobot_checkpoint(tmp_path / "vqbet", type="vqbet")), robot)
    assert unknown.value.what == "LeRobot policy type 'vqbet' is not supported yet."
    assert "@neura.policy" in " ".join(unknown.value.details)


def test_a_container_is_an_image_pinned_by_digest_with_a_declaration(
    tmp_path: Path, robot: dict[str, Any]
) -> None:
    image = "registry.acme.example/picker@sha256:" + "b" * 64
    with pytest.raises(CliError, match="declares what it reads"):
        sources.load(image, robot)
    declaration = tmp_path / "picker.yaml"
    declaration.write_text(
        yaml.safe_dump({"chunk_length": 20, "cameras": {"wrist": [224, 224]}, "language": True}),
        encoding="utf-8",
    )
    source = sources.load(image, robot, declare=declaration)
    assert source.kind == "container"
    assert source.runtime_image == image
    artifact = source.artifact(robot, "picker")
    assert artifact["spec"]["runtime_image"]["reference"] == image
    assert artifact["spec"]["adapter"]["name"] == "container"
    assert [i["kind"] for i in artifact["spec"]["inputs"]] == ["state", "image", "language"]
    declaration.write_text(yaml.safe_dump({"chunk_length": 20, "colour": "red"}), encoding="utf-8")
    with pytest.raises(CliError, match="unknown fields: colour"):
        sources.load(image, robot, declare=declaration)
    with pytest.raises(CliError, match="not a policy Neura can read"):
        sources.load("registry.acme.example/picker:latest", robot)


def test_policy_classes_need_an_act_method() -> None:
    import neura

    with pytest.raises(authoring.AuthoringError, match="act"):

        @neura.policy(chunk_length=4)
        class Nothing:
            pass


# --- keys and files ----------------------------------------------------------------------------


def test_the_publisher_key_is_made_once_and_kept_private() -> None:
    config = Config.load()
    assert keys.load(config, create=False) is None
    key = keys.load(config, create=True)
    assert key is not None
    path = keys.path(config)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = keys.load(config, create=False)
    assert again is not None
    assert keys.public_hex(again) == keys.public_hex(key)
    digest = "sha256:" + "c" * 64
    assert signing.verify(digest, signing.sign(digest, again))


def test_files_reach_a_local_site_by_digest_and_the_originals_are_untouched(
    tmp_path: Path, robot: dict[str, Any]
) -> None:
    source = sources.load(str(python_policy(tmp_path / "hold")), robot)
    original = tmp_path / "hold" / "hold.py"
    mode = stat.S_IMODE(original.stat().st_mode)
    store = tmp_path / "site-weights"
    assert weights.to_directory(source.files, store) == (3, 0)
    assert weights.to_directory(source.files, store) == (0, 3)
    for item in source.files:
        blob = store / "sha256" / item.digest.removeprefix("sha256:")
        assert hashlib.sha256(blob.read_bytes()).hexdigest() == item.digest.removeprefix("sha256:")
    assert stat.S_IMODE(original.stat().st_mode) == mode
    assert not [p for p in (store / "sha256").iterdir() if p.name.startswith(".incoming")]


# --- neura check -------------------------------------------------------------------------------


def test_neura_check_runs_a_python_policy_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = python_policy(tmp_path / "hold")
    profile = EXAMPLES / "so101.robot.yaml"
    assert main(["check", str(path), "--profile", str(profile), "--run"]) == 0
    printed = capsys.readouterr().out
    assert "Fits so101-follower" in printed
    assert "reads state, front 64x48; returns 10 actions" in printed
    assert "Ran on this machine" in printed
    assert "Keeps up (measured on this machine)" in printed

    broken = python_policy(tmp_path / "broken", HOLD.replace("(10, 1)", "(3, 1)"))
    assert main(["check", str(broken), "--profile", str(profile), "--run"]) == 1
    assert "wrong shape" in capsys.readouterr().out

    assert main(["--json", "check", str(path), "--profile", str(profile)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["fits"] is True
    assert result["artifact"]["spec"]["adapter"]["name"] == "python"


def test_errors_say_what_happened_and_what_to_do(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["robots"]) == 1
    error = capsys.readouterr().err
    assert "You are not signed in." in error
    assert "neura login" in error
    assert main(["--json", "robots"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["message"] == "You are not signed in."


# --- a local site ------------------------------------------------------------------------------


def test_a_local_site_is_a_development_bench_listening_on_loopback_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = LocalSite.at("lab")
    local.configure(
        api_url="http://127.0.0.1:8080",
        bench=True,
        simulated_robots=2,
        trusted_keys=["ab" * 32],
        mock_policies=True,
    )
    env = local.environment()
    assert local.directory == tmp_path / "data" / "neura" / "sites" / "lab"
    assert stat.S_IMODE(local.directory.stat().st_mode) == 0o700
    assert env["NEURA_AGENT_DEVELOPMENT"] == "true"
    assert env["NEURA_AGENT_BENCH"] == "true"
    assert env["NEURA_AGENT_SIMULATED_ROBOTS"] == "2"
    assert env["NEURA_AGENT_CONTROL_PLANE_INSECURE"] == "true"
    assert env["NEURA_AGENT_GATEWAY_BACKEND"] == "simulated"
    assert json.loads(env["NEURA_AGENT_ARTIFACT_SOURCES"]) == [str(local.weights_dir)]
    assert json.loads(env["NEURA_AGENT_TRUSTED_PUBLISHER_KEYS"]) == ["ab" * 32]
    for key in ("GATEWAY_LISTEN", "GATEWAY_STATUS_LISTEN", "CAPTURE_LISTEN"):
        assert env[f"NEURA_AGENT_{key}"].startswith("127.0.0.1:")
    token = local.operator_token()
    assert hashlib.sha256(token.encode()).hexdigest() == env["NEURA_AGENT_OPERATOR_TOKEN_SHA256"]
    assert stat.S_IMODE(local.token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(local.env_file.stat().st_mode) == 0o600
    # The agent accepts these settings.
    from neura_agent.settings import AgentSettings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = AgentSettings()
    assert settings.bench
    assert settings.simulated_robots == 2
