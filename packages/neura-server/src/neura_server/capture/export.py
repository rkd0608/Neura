"""Turning site episodes into a LeRobot dataset (design §28.4), the format training reads.

Episodes are recorded in robot order and units. A dataset is built for one model: its state
inputs, camera inputs and actions, in the model's order and units, through the same execution
profile mappings serving uses (``ExecutionBinding``), so what training sees is what the model
sees when it runs. Camera frames recorded every few periods are held between recordings; an
episode that is incomplete, or whose robot, timing or cameras do not fit the model, is left out
and reported.

Needs LeRobot's dataset support (``lerobot[dataset]``, in the ``training`` dependency group):
it runs in the training job, never in the gateway or the agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_runtime.binding import ExecutionBinding
from neura_runtime.capture.episode import Episode, Steps, read_frame


@dataclass(slots=True)
class ExportReport:
    root: Path
    repo_id: str
    fps: int
    episodes: list[str] = field(default_factory=list)
    frames: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "repo_id": self.repo_id,
            "fps": self.fps,
            "episodes": len(self.episodes),
            "episode_ids": self.episodes,
            "frames": self.frames,
            "skipped": self.skipped,
            "features": {
                key: {"dtype": value["dtype"], "shape": list(value["shape"])}
                for key, value in self.features.items()
            },
        }


def features(binding: ExecutionBinding, *, videos: bool = False) -> dict[str, Any]:
    """The dataset's features for this model: LeRobot's names are the model's input names."""
    out: dict[str, Any] = {}
    for layout in binding.state_layouts:
        out[layout.input_name] = {
            "dtype": "float32",
            "shape": (len(layout.feature_names),),
            "names": list(layout.feature_names),
        }
    for image in binding.image_layouts:
        out[image.input_name] = {
            "dtype": "video" if videos else "image",
            "shape": (image.height, image.width, 3),
            "names": ["height", "width", "channels"],
        }
    out["action"] = {
        "dtype": "float32",
        "shape": (binding.action_dim,),
        "names": [f.name for f in binding.artifact.spec.outputs.features],
    }
    return out


def fits(episode: Episode, binding: ExecutionBinding) -> str | None:
    """Why this episode cannot train this model, or None."""
    manifest = episode.manifest
    if not manifest.get("complete", False):
        return "incomplete: the robot dropped data while recording it"
    if manifest["control_period_us"] != binding.control_period_us:
        return (
            f"recorded at a {manifest['control_period_us']} us period, the model runs at "
            f"{binding.control_period_us} us"
        )
    commands = [c.name for c in binding.robot.spec.command_interface]
    if manifest["command_names"] != commands:
        return "the robot's commands differ from this model's robot profile"
    needed = {name for layout in binding.state_layouts for name in layout.feature_names}
    if not needed <= set(manifest["state_names"]):
        return "the robot's state lacks features the model reads"
    cameras = {c["role"]: c for c in manifest["cameras"]}
    for image in binding.image_layouts:
        camera = cameras.get(image.camera_role)
        if camera is None:
            return f"camera {image.camera_role!r} was not recorded"
        if (camera["width"], camera["height"]) != (image.width, image.height):
            return f"camera {image.camera_role!r} was recorded at another size"
    if binding.language_input is not None and not manifest.get("instruction"):
        return "the model needs an instruction and the episode has none"
    if manifest["steps"] == 0:
        return "no commands were recorded"
    return None


def frames(
    episode: Episode, binding: ExecutionBinding, steps: Steps | None = None
) -> list[dict[str, Any]]:
    """The episode as model-unit frames, one per control period from the first camera frame."""
    steps = steps or episode.steps()
    names = list(episode.manifest["state_names"])
    model_actions = binding.action_transform.to_model(steps.action).astype(np.float32)
    states: dict[str, NDArray[np.float32]] = {}
    for layout in binding.state_layouts:
        columns = [names.index(n) for n in layout.feature_names]
        states[layout.input_name] = (steps.state[:, columns] * layout.factors).astype(np.float32)
    first = int(np.argmax(steps.frame >= 0)) if (steps.frame >= 0).any() else len(steps.frame)
    if binding.image_layouts and first == len(steps.frame):
        return []
    start = first if binding.image_layouts else 0
    out: list[dict[str, Any]] = []
    current: dict[str, NDArray[np.uint8]] = {}
    cameras = {c["role"]: c for c in episode.manifest["cameras"]}
    for i in range(start, len(steps.t_us)):
        if not all(np.isfinite(states[k][i]).all() for k in states):
            raise ValueError(f"step {i} has a missing state reading")
        frame_index = int(steps.frame[i])
        if frame_index >= 0:
            for image in binding.image_layouts:
                camera = cameras[image.camera_role]
                offsets = steps.frame_offsets[image.camera_role]
                raw = read_frame(
                    episode.directory / f"camera-{image.camera_role}.frames",
                    int(offsets[frame_index]),
                )
                current[image.input_name] = np.frombuffer(raw, np.uint8).reshape(
                    camera["height"], camera["width"], 3
                )
        frame: dict[str, Any] = {key: values[i] for key, values in states.items()}
        frame.update(current)
        frame["action"] = model_actions[i]
        out.append(frame)
    return out


def usable_frames(
    episode: Episode, binding: ExecutionBinding, steps: Steps | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """The episode's model-unit frames and None, or no frames and why the episode cannot be used
    for this model: the one rule for training (``export_lerobot``) and replay alike."""
    problem = fits(episode, binding)
    if problem is not None:
        return [], problem
    try:
        rows = frames(episode, binding, steps)
    except ValueError as exc:  # a missing state reading
        return [], str(exc)
    if not rows:
        return [], "no camera frame was recorded"
    return rows, None


def export_lerobot(
    episodes: list[Episode],
    binding: ExecutionBinding,
    root: Path,
    *,
    repo_id: str = "neura/site",
    task: str = "",
    videos: bool = False,
) -> ExportReport:
    """Write ``episodes`` as a LeRobot dataset at ``root`` (which must not exist) for the
    model ``binding`` names."""
    try:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
    except ImportError as exc:  # pragma: no cover - depends on the installed extras
        raise RuntimeError(
            "exporting a dataset needs LeRobot's dataset support (lerobot[dataset])"
        ) from exc

    fps = round(1e6 / binding.control_period_us)
    spec = features(binding, videos=videos)
    report = ExportReport(root=root, repo_id=repo_id, fps=fps, features=spec)
    usable: list[tuple[Episode, list[dict[str, Any]]]] = []
    for episode in episodes:
        rows, problem = usable_frames(episode, binding)
        if problem is not None:
            report.skipped.append({"episode_id": episode.episode_id, "reason": problem})
        else:
            usable.append((episode, rows))
    if not usable:
        raise ValueError(
            "no episode can train this model: "
            + "; ".join(f"{s['episode_id']}: {s['reason']}" for s in report.skipped[:5])
        )
    dataset = LeRobotDataset.create(
        repo_id=repo_id,
        fps=fps,
        features=spec,
        root=root,
        robot_type=binding.robot.metadata.name,
        use_videos=videos,
    )
    for episode, rows in usable:
        text = episode.manifest.get("instruction") or task or "neura episode"
        for row in rows:
            dataset.add_frame({**row, "task": text})
        dataset.save_episode()
        report.episodes.append(episode.episode_id)
        report.frames += len(rows)
    dataset.finalize()
    return report
