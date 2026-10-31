"""Evaluating a candidate model against the model it would replace (design §28.3).

Two suites, both run at the site, in the worker sandbox:

- **Simulation**: both models on the same scenes (the same seeds) of a task the site can simulate,
  in lockstep (the world waits for the policy, as LeRobot evaluates its published policies), so
  the comparison measures the models, not the computer. The verdict is the paired
  non-inferiority test of ``stats``.
- **Replay**: both models on observations the site's robots recorded (``neura-episode/1``). Each
  model predicts a chunk from a recorded observation, compared with what the robot then did, in
  the model's units, normalized by each action's range. It catches a candidate that drifts from
  behavior that worked (a regression check on real data); it cannot show a candidate is better,
  since the baseline produced those actions.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from neura_contracts.stats import compare
from neura_runtime.binding import ExecutionBinding
from neura_runtime.capture.episode import Episode

Progress = Callable[[str], None]
SCENES: dict[str, Literal["transfer_cube", "insertion"]] = {
    "aloha_transfer_cube": "transfer_cube",
    "aloha_insertion": "insertion",
}


@dataclass(frozen=True, slots=True)
class SimulationSuite:
    scene: Literal["aloha_transfer_cube", "aloha_insertion"] = "aloha_transfer_cube"
    episodes: int = 50
    first_seed: int = 1000
    max_steps: int = 400
    # The largest drop in success rate still called no worse, agreed before the run.
    margin: float = 0.10

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SimulationSuite:
        suite = cls(**{k: v for k, v in data.items() if k != "kind"})
        if suite.scene not in SCENES:
            raise ValueError(f"scene must be one of {sorted(SCENES)}")
        if not 1 <= suite.episodes <= 10_000 or not 1 <= suite.max_steps <= 100_000:
            raise ValueError("episodes and max_steps must be positive and bounded")
        if not 0 <= suite.margin < 1:
            raise ValueError("margin is a difference in success rate, from 0 to 1")
        return suite


@dataclass(frozen=True, slots=True)
class ReplaySuite:
    # Actions compared per prediction; None: the actions a robot executes between refills.
    horizon: int | None = None
    # Predict at every Nth recorded camera frame.
    stride: int = 5
    max_episodes: int = 200
    # The candidate passes when its error on successful episodes is at most the baseline's times
    # (1 + relative) plus absolute (normalized units: 0.01 is 1% of an action's range).
    relative: float = 0.25
    absolute: float = 0.01
    batch_size: int = 8

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ReplaySuite:
        suite = cls(**{k: v for k, v in data.items() if k != "kind"})
        if suite.stride < 1 or suite.max_episodes < 1 or suite.batch_size < 1:
            raise ValueError("stride, max_episodes and batch_size must be positive")
        if suite.horizon is not None and suite.horizon < 1:
            raise ValueError("horizon must be positive")
        return suite


# --- simulation ---------------------------------------------------------------------------------


def evaluate_simulation(
    candidate: Path,
    baseline: Path,
    suite: SimulationSuite,
    *,
    device: str = "cpu",
    progress: Progress = lambda _: None,
) -> dict[str, Any]:
    """Both checkpoints on the suite's scenes; the paired comparison and every scene's result."""
    from neura_bench.evidence.local import run_eval_protocol
    from neura_bench.evidence.policy import LocalPolicy
    from neura_bench.evidence.results import summarize
    from neura_robots.aloha import AlohaSim

    seeds = list(range(suite.first_seed, suite.first_seed + suite.episodes))
    sim = AlohaSim(SCENES[suite.scene])
    try:
        results = {}
        for role, checkpoint in (("candidate", candidate), ("baseline", baseline)):
            progress(f"{role}: {suite.episodes} scenes")
            policy = LocalPolicy(checkpoint, device=device)
            results[role] = run_eval_protocol(
                policy,
                sim,
                seeds,
                max_steps=suite.max_steps,
                progress=_prefixed(progress, role),
            )
    finally:
        sim.close()
    pairs = [
        (c.success, b.success)
        for c, b in zip(results["candidate"], results["baseline"], strict=True)
    ]
    comparison = compare(pairs, margin=suite.margin)
    return {
        "kind": "simulation",
        "suite": {"kind": "simulation", **_fields(suite)},
        "candidate": _summary(summarize(results["candidate"])),
        "baseline": _summary(summarize(results["baseline"])),
        "comparison": comparison.to_json(),
        "verdict": comparison.verdict.value,
        "scenes": [
            {
                "seed": c.seed,
                "candidate": {"success": c.success, "steps": c.steps},
                "baseline": {"success": b.success, "steps": b.steps},
            }
            for c, b in zip(results["candidate"], results["baseline"], strict=True)
        ],
    }


def _prefixed(progress: Progress, prefix: str) -> Progress:
    return lambda line: progress(f"{prefix}: {line}")


def _summary(summary: dict[str, Any]) -> dict[str, Any]:
    keep = ("episodes", "successes", "success_rate", "success_rate_95ci", "mean_max_reward")
    return {k: summary[k] for k in keep if k in summary}


def _fields(suite: Any) -> dict[str, Any]:
    return {name: getattr(suite, name) for name in suite.__dataclass_fields__}


# --- replay -----------------------------------------------------------------------------------


Predict = Callable[[Sequence[Any]], list[NDArray[np.float32]]]


@dataclass(slots=True)
class _Errors:
    by_outcome: dict[str, list[float]] = field(default_factory=dict)

    def add(self, outcome: str, value: float) -> None:
        self.by_outcome.setdefault(outcome, []).append(value)

    def to_json(self) -> dict[str, Any]:
        def stats(values: list[float]) -> dict[str, float | int]:
            array = np.array(values)
            return {
                "samples": len(values),
                "mean": round(float(array.mean()), 5),
                "p95": round(float(np.percentile(array, 95)), 5),
            }

        everything = [v for values in self.by_outcome.values() for v in values]
        out: dict[str, Any] = {"all": stats(everything)} if everything else {"all": {"samples": 0}}
        out.update({k: stats(v) for k, v in sorted(self.by_outcome.items())})
        return out


def action_ranges(binding: ExecutionBinding) -> NDArray[np.float64]:
    """Each model action's range, in model units, from the robot's declared command ranges."""
    transform = binding.action_transform
    corners = transform.to_model(np.stack([transform.low, transform.high]))
    span = np.abs(corners[1] - corners[0])
    return np.where(span > 0, span, 1.0)


Sample = tuple[str, str, Any, NDArray[np.float32]]


def replay_samples(
    episodes: Sequence[Episode], binding: ExecutionBinding, suite: ReplaySuite
) -> tuple[list[Sample], dict[str, int]]:
    """(episode, outcome, model inputs, actions the robot executed next, in model units) at
    every ``stride``-th recorded frame of each episode that can be used for the model, and how
    many episodes were left out, by why (as a dataset export leaves them out)."""
    from neura_runtime.policy import ModelInputs
    from neura_server.capture.export import usable_frames

    horizon = suite.horizon or max(
        1,
        binding.profile.spec.chunk.actions_per_chunk
        - binding.profile.spec.chunk.refill_threshold_actions,
    )
    samples: list[Sample] = []
    skipped: Counter[str] = Counter()
    for episode in list(episodes)[: suite.max_episodes]:
        rows, problem = usable_frames(episode, binding)
        if problem is not None:
            skipped[problem] += 1
            continue
        steps = episode.steps()
        offset = len(steps.t_us) - len(rows)  # rows start at the first recorded frame
        recorded = [i for i in range(len(rows)) if steps.frame[offset + i] >= 0]
        outcome = str(episode.manifest["outcome"]["label"])
        for i in recorded[:: suite.stride]:
            if i + horizon > len(rows):
                break
            inputs = ModelInputs(
                state={
                    layout.input_name: rows[i][layout.input_name]
                    for layout in binding.state_layouts
                },
                images={
                    image.camera_role: rows[i][image.input_name] for image in binding.image_layouts
                },
                instruction=str(episode.manifest.get("instruction", "")),
                frozen_prefix=None,
            )
            executed = np.stack([rows[i + k]["action"] for k in range(horizon)])
            samples.append((episode.episode_id, outcome, inputs, executed))
    return samples, dict(skipped)


def replay_errors(
    predict: Predict, samples: Sequence[Sample], ranges: NDArray[np.float64], batch_size: int
) -> _Errors:
    errors = _Errors()
    for start in range(0, len(samples), batch_size):
        batch = samples[start : start + batch_size]
        chunks = predict([inputs for _, _, inputs, _ in batch])
        for (_, outcome, _, executed), chunk in zip(batch, chunks, strict=True):
            horizon = min(len(executed), len(chunk))
            diff = np.abs(chunk[:horizon].astype(np.float64) - executed[:horizon]) / ranges
            errors.add(outcome, float(diff.mean()))
    return errors


def evaluate_replay(
    episodes: Sequence[Episode],
    candidate: Predict,
    candidate_binding: ExecutionBinding,
    suite: ReplaySuite,
    *,
    baseline: Predict | None = None,
    baseline_binding: ExecutionBinding | None = None,
) -> dict[str, Any]:
    samples, skipped = replay_samples(episodes, candidate_binding, suite)
    if not samples:
        if sum(skipped.values()) < min(len(episodes), suite.max_episodes):
            raise ValueError(
                "the recorded episodes are shorter than the actions compared per prediction"
            )
        raise ValueError(
            "no recorded episode can be replayed on the candidate model: " + "; ".join(skipped)
        )
    ranges = action_ranges(candidate_binding)
    report: dict[str, Any] = {
        "kind": "replay",
        "suite": {"kind": "replay", **_fields(suite)},
        "episodes": len({episode_id for episode_id, *_ in samples}),
        "episodes_offered": len(episodes),
        "skipped": skipped,  # episodes left out, by why: counts, never their content
        "samples": len(samples),
        "candidate": replay_errors(candidate, samples, ranges, suite.batch_size).to_json(),
    }
    if baseline is not None:
        base_samples = (
            samples
            if baseline_binding is None or baseline_binding is candidate_binding
            else replay_samples(episodes, baseline_binding, suite)[0]
        )
        base_ranges = ranges if baseline_binding is None else action_ranges(baseline_binding)
        report["baseline"] = replay_errors(
            baseline, base_samples, base_ranges, suite.batch_size
        ).to_json()
        report["verdict"] = replay_verdict(report["candidate"], report["baseline"], suite)
    return report


def replay_verdict(candidate: dict[str, Any], baseline: dict[str, Any], suite: ReplaySuite) -> str:
    """non_inferior when the candidate reproduces successful episodes nearly as well as the
    baseline that produced them; inconclusive without successful episodes."""
    group = "success"
    if candidate.get(group, {}).get("samples", 0) == 0 or group not in baseline:
        return "inconclusive"
    allowed = baseline[group]["mean"] * (1 + suite.relative) + suite.absolute
    value = candidate[group]["mean"]
    if math.isnan(value):
        return "inconclusive"
    return "non_inferior" if value <= allowed else "inferior"


def adapter_predict(binding: ExecutionBinding, checkpoint: Path, *, device: str = "cpu") -> Predict:
    """The checkpoint behind the adapter serving uses: the same preprocessing and units."""
    from neura_adapters import load_adapter

    precision = binding.profile.spec.runtime.precision.value
    if precision not in ("fp32", "fp16", "bf16"):
        raise ValueError(f"precision {precision} is not supported")
    adapter = load_adapter(
        binding.artifact.spec.adapter.name,
        binding,
        checkpoint=checkpoint,
        device=device,
        precision=precision,  # type: ignore[arg-type]
    )

    def predict(batch: Sequence[Any]) -> list[NDArray[np.float32]]:
        return adapter.infer(list(batch))

    return predict
