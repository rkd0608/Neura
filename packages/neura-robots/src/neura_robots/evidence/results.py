"""Episode results and their summaries."""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from neura_contracts.stats import wilson

Z_95_TWO_SIDED = 1.959964


@dataclass(slots=True)
class EpisodeResult:
    system: str
    seed: int
    success: bool
    max_reward: int
    steps: int  # control periods of world time until success or the end of the episode
    inference_ms: list[float] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    faults: list[str] = field(default_factory=list)
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        if self.inference_ms:
            ordered = sorted(self.inference_ms)
            data["inference_ms"] = {
                "count": len(ordered),
                "median": round(ordered[len(ordered) // 2], 2),
                "max": round(ordered[-1], 2),
            }
        return data


def summarize(results: list[EpisodeResult]) -> dict[str, Any]:
    n = len(results)
    successes = sum(r.success for r in results)
    low, high = wilson(successes, n, Z_95_TWO_SIDED)
    inference = sorted(ms for r in results for ms in r.inference_ms)
    summary: dict[str, Any] = {
        "episodes": n,
        "successes": successes,
        "success_rate": round(successes / n, 4) if n else None,
        "success_rate_95ci": [round(low, 4), round(high, 4)],
        "mean_max_reward": round(sum(r.max_reward for r in results) / n, 3) if n else None,
        "faulted_episodes": sum(bool(r.faults) for r in results),
        # Each fault reason, in how many episodes it was given.
        "faults": dict(Counter(f for r in results for f in dict.fromkeys(r.faults))),
        # Each counter's median over the episodes (an episode without it counts 0).
        "counter_medians": {
            key: statistics.median(r.counters.get(key, 0) for r in results)
            for key in sorted({k for r in results for k in r.counters})
        },
    }
    if inference:
        summary["inference_ms"] = {
            "median": round(inference[len(inference) // 2], 2),
            "p95": round(inference[min(len(inference) - 1, int(len(inference) * 0.95))], 2),
        }
    return summary
