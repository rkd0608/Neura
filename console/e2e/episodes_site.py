"""The site agent's local API with two recorded episodes, for the episode viewer's browser test.

    uv run python console/e2e/episodes_site.py <port>

Prints READY once serving, and runs until terminated. The operator credential is ``TOKEN``.
"""

from __future__ import annotations

import hashlib
import signal
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import uvicorn
import yaml

from neura_agent.app import create_app
from neura_agent.settings import AgentSettings
from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, parse_document
from neura_runtime.binding import ExecutionBinding
from neura_runtime.capture.episode import canonical
from neura_runtime.capture.testing import synthetic_episode
from neura_server.capture.store import EpisodeStore

TOKEN = "ntop_" + "v" * 43
EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "contracts"


class Audit:
    def write(self, action: str, **fields: Any) -> None:
        del action, fields


def binding() -> ExecutionBinding:
    artifact, robot, profile = (
        yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))
        for name in ("act-so101.artifact.yaml", "so101.robot.yaml", "act-so101-30hz.profile.yaml")
    )
    model, arm = parse_document(artifact), parse_document(robot)
    assert isinstance(model, ModelArtifact)
    assert isinstance(arm, RobotProfile)
    profile["spec"]["binding"] = {"model_artifact": model.digest(), "robot_profile": arm.digest()}
    execution = parse_document(profile)
    assert isinstance(execution, ExecutionProfile)
    return ExecutionBinding(model, arm, execution)


def exit_on_sigterm(*_: object) -> None:
    """SIGTERM ends this process the way Ctrl-C does, through its cleanup. uvicorn handles the
    signal, stops serving, then raises it again: by default that kills the process first."""
    raise SystemExit(0)


def main(port: int) -> None:
    signal.signal(signal.SIGTERM, exit_on_sigterm)
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        store = EpisodeStore(work / "episodes")
        for seed, outcome in enumerate(["success", "failure"]):
            episode = synthetic_episode(
                work / "recorded",
                binding(),
                robot_id="arm-1",
                revision_id="rev_1",
                steps=90,
                image_stride=3,
                outcome=outcome,
                seed=seed,
            )
            upload = store.begin(canonical(episode.manifest), robot_id="arm-1")
            for name in sorted(episode.manifest["files"]):
                upload.write(name, (episode.directory / name).read_bytes())
            assert upload.commit()
        runtime = SimpleNamespace(
            core=SimpleNamespace(capture=SimpleNamespace(store=store), audit=Audit())
        )
        settings = AgentSettings(
            state_dir=work / "agent",
            operator_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        )
        app = create_app(settings, runtime=runtime, start_probe=False)  # type: ignore[arg-type]
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        print("READY", flush=True)
        server.run()


if __name__ == "__main__":
    main(int(sys.argv[1]))
