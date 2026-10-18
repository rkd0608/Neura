"""Evaluation and training jobs, as the site agent runs them (design §28.3, §28.4).

The agent writes a job's spec (its kind, the contract documents of the models involved, the
suite or recipe) and runs ``python -m neura_learn`` in the worker sandbox: customer model code
is untrusted, so a job gets no network, its inputs read-only and one output directory. Progress
goes to standard output as JSON lines; the result is ``<out>/report.json`` (and, for training,
``<out>/checkpoint``).

    neura-learn evaluate --spec FILE --candidate DIR [--baseline DIR] [--episodes DIR] --out DIR
    neura-learn train --spec FILE --base DIR --episodes DIR --out DIR
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, parse_document
from neura_runtime.binding import ExecutionBinding
from neura_runtime.capture.episode import Episode, EpisodeError, open_episode

log = logging.getLogger("neura.learn")


def say(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "at": round(time.time(), 3), **fields}), flush=True)


def binding_from(documents: dict[str, Any]) -> ExecutionBinding:
    artifact = parse_document(documents["model_artifact"])
    robot = parse_document(documents["robot_profile"])
    profile = parse_document(documents["execution_profile"])
    if not (
        isinstance(artifact, ModelArtifact)
        and isinstance(robot, RobotProfile)
        and isinstance(profile, ExecutionProfile)
    ):
        raise ValueError(
            "a model's documents are a ModelArtifact, a RobotProfile and an ExecutionProfile"
        )
    return ExecutionBinding(artifact, robot, profile)


def load_episodes(directory: Path) -> list[Episode]:
    """Every intact episode under ``directory`` (one sub-directory each), oldest first; labels
    are read from ``labels.json`` when the agent wrote one (the site's labels win over the
    robot's)."""
    labels_file = directory / "labels.json"
    labels: dict[str, Any] = (
        json.loads(labels_file.read_text(encoding="utf-8")) if labels_file.is_file() else {}
    )
    episodes = []
    for path in sorted(p for p in directory.iterdir() if p.is_dir()):
        try:
            episode = open_episode(path)
        except EpisodeError as exc:
            log.warning("skipping %s: %s", path.name, exc)
            continue
        if episode.episode_id in labels:
            episode = Episode(
                episode.directory, {**episode.manifest, "outcome": labels[episode.episode_id]}
            )
        episodes.append(episode)
    return sorted(episodes, key=lambda e: (str(e.manifest.get("started_at", "")), e.episode_id))


def run_evaluate(args: argparse.Namespace, spec: dict[str, Any]) -> dict[str, Any]:
    from neura_learn.evaluate import (
        ReplaySuite,
        SimulationSuite,
        adapter_predict,
        evaluate_replay,
        evaluate_simulation,
    )

    suite = spec["suite"]
    device = spec.get("device", "cpu")
    if suite.get("kind") == "simulation":
        if args.baseline is None:
            raise ValueError("a simulation evaluation compares the candidate with a baseline")
        return evaluate_simulation(
            args.candidate,
            args.baseline,
            SimulationSuite.from_json(suite),
            device=device,
            progress=lambda line: say("progress", message=line),
        )
    if suite.get("kind") == "replay":
        if args.episodes is None:
            raise ValueError("a replay evaluation needs recorded episodes")
        episodes = load_episodes(args.episodes)
        say("progress", message=f"{len(episodes)} recorded episodes")
        candidate_binding = binding_from(spec["candidate"]["documents"])
        baseline_binding = None
        baseline = None
        if args.baseline is not None:
            baseline_binding = binding_from(spec["baseline"]["documents"])
            baseline = adapter_predict(baseline_binding, args.baseline, device=device)
        return evaluate_replay(
            episodes,
            adapter_predict(candidate_binding, args.candidate, device=device),
            candidate_binding,
            ReplaySuite.from_json(suite),
            baseline=baseline,
            baseline_binding=baseline_binding,
        )
    raise ValueError(f"unknown suite kind {suite.get('kind')!r}: simulation or replay")


def run_train(args: argparse.Namespace, spec: dict[str, Any]) -> dict[str, Any]:
    from neura_learn.train import Recipe, fine_tune
    from neura_server.capture.export import export_lerobot

    recipe = Recipe.from_json(spec.get("recipe", {}))
    binding = binding_from(spec["base"]["documents"])
    episodes = load_episodes(args.episodes)
    say("progress", message=f"exporting {len(episodes)} episodes as a LeRobot dataset")
    with tempfile.TemporaryDirectory(prefix="neura-dataset-") as work:
        root = Path(work) / "dataset"
        export = export_lerobot(episodes, binding, root)
        say(
            "progress",
            message=f"dataset: {len(export.episodes)} episodes, {export.frames} frames"
            + (f", {len(export.skipped)} left out" if export.skipped else ""),
        )
        report = fine_tune(
            args.base,
            root,
            args.out,
            recipe,
            device=spec.get("device", "cpu"),
            progress=lambda line: say("progress", message=line),
        )
    return {
        "kind": "training",
        "recipe": recipe.to_json(),
        "dataset": {
            "episodes": len(export.episodes),
            "frames": export.frames,
            "fps": export.fps,
            "skipped": len(export.skipped),
            "skipped_reasons": sorted({s["reason"] for s in export.skipped}),
        },
        "training": report.to_json(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neura-learn",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--spec", type=Path, required=True)
    evaluate.add_argument("--candidate", type=Path, required=True, help="its checkpoint")
    evaluate.add_argument("--baseline", type=Path, default=None, help="its checkpoint")
    evaluate.add_argument("--episodes", type=Path, default=None)
    evaluate.add_argument("--out", type=Path, required=True)
    train = sub.add_parser("train")
    train.add_argument("--spec", type=Path, required=True)
    train.add_argument("--base", type=Path, required=True, help="the base checkpoint")
    train.add_argument("--episodes", type=Path, required=True)
    train.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    try:
        if args.command == "evaluate":
            args.out.mkdir(parents=True, exist_ok=True)
            result = run_evaluate(args, spec)
        else:
            result = run_train(args, spec)
    except Exception as exc:
        log.exception("%s failed", args.command)
        say("failed", error=f"{type(exc).__name__}: {exc}"[:2000])
        return 1
    (args.out / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    say("done", report=str(args.out / "report.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
