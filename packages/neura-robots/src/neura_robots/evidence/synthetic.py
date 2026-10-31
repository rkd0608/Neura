"""A LeRobot ACT checkpoint shaped like the published ALOHA ones (the top camera at 640x480, the
14-value state, 14 actions, chunks of 100), with random weights: for testing the evidence
pipeline where the published checkpoints cannot be downloaded. It never succeeds at the task,
and nothing measured with it is evidence of task success."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from neura_contracts import ModelArtifact, parse_document
from neura_robots.aloha.contracts import artifact, robot_profile

CONFIG = {
    "type": "act",
    "chunk_size": 100,
    "input_features": {
        "observation.images.top": {"type": "VISUAL", "shape": [3, 480, 640]},
        "observation.state": {"type": "STATE", "shape": [14]},
    },
    "output_features": {"action": {"type": "ACTION", "shape": [14]}},
}


def write_checkpoint(directory: Path, *, size: str = "tiny", seed: int = 0) -> Path:
    from neura_adapters.synthetic import write_act_checkpoint

    with tempfile.TemporaryDirectory() as scratch:
        draft_dir = Path(scratch)
        (draft_dir / "config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        draft = parse_document(artifact(draft_dir, name="act-aloha-synthetic", source="synthetic"))
    assert isinstance(draft, ModelArtifact)
    ranges = [(c["min"], c["max"]) for c in robot_profile()["spec"]["command_interface"]]
    return write_act_checkpoint(draft, directory, size=size, seed=seed, action_ranges=ranges)  # type: ignore[arg-type]
