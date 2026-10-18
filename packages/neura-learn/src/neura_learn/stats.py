"""The statistics a release decision rests on (design §28.3).

A candidate model is compared with the model it would replace on the same scenes (the same
seeds), so the comparison is paired: each scene is a success or failure for each model. The
verdict is a non-inferiority test on the difference in success rates, candidate minus baseline,
with a margin agreed before the run:

- ``non_inferior``: the difference's one-sided 95% lower bound is above ``-margin``;
- ``superior``: it is above 0 as well;
- ``inferior``: the upper bound is below ``-margin`` (clearly worse);
- ``inconclusive``: neither; more scenes would decide.

Intervals are Wilson score intervals for one rate and Newcombe's hybrid score intervals for a
difference (1998: method 10 for independent samples, its paired counterpart for paired ones),
both well behaved at the 0% and 100% rates small evaluations produce.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

Z_95_ONE_SIDED = 1.6448536269514722


class Verdict(StrEnum):
    SUPERIOR = "superior"
    NON_INFERIOR = "non_inferior"
    INCONCLUSIVE = "inconclusive"
    INFERIOR = "inferior"


def wilson(successes: int, n: int, z: float = Z_95_ONE_SIDED) -> tuple[float, float]:
    """Wilson score interval for a proportion."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    denominator = 1 + z * z / n
    return max(0.0, (centre - spread) / denominator), min(1.0, (centre + spread) / denominator)


def difference_unpaired(
    s1: int, n1: int, s2: int, n2: int, z: float = Z_95_ONE_SIDED
) -> tuple[float, float, float]:
    """p1 - p2 and its interval (Newcombe 1998, method 10), for independent samples."""
    p1, p2 = s1 / n1, s2 / n2
    l1, u1 = wilson(s1, n1, z)
    l2, u2 = wilson(s2, n2, z)
    delta = p1 - p2
    low = delta - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    high = delta + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return delta, max(-1.0, low), min(1.0, high)


def difference_paired(
    pairs: Sequence[tuple[bool, bool]], z: float = Z_95_ONE_SIDED
) -> tuple[float, float, float]:
    """p1 - p2 and its interval for paired outcomes (candidate, baseline) on the same scenes:
    Newcombe's hybrid score interval with the correlation between the two models' outcomes."""
    n = len(pairs)
    if n == 0:
        return 0.0, -1.0, 1.0
    a = sum(1 for c, b in pairs if c and b)
    b_only_c = sum(1 for c, b in pairs if c and not b)  # the candidate succeeded where the
    c_only_b = sum(1 for c, b in pairs if b and not c)  # baseline failed, and the reverse
    d = n - a - b_only_c - c_only_b
    p1 = (a + b_only_c) / n
    p2 = (a + c_only_b) / n
    l1, u1 = wilson(a + b_only_c, n, z)
    l2, u2 = wilson(a + c_only_b, n, z)
    margins = (a + b_only_c) * (c_only_b + d) * (a + c_only_b) * (b_only_c + d)
    phi = (a * d - b_only_c * c_only_b) / math.sqrt(margins) if margins else 0.0
    delta = p1 - p2
    low_spread = (p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2
    high_spread = (u1 - p1) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2
    low = delta - math.sqrt(max(0.0, low_spread))
    high = delta + math.sqrt(max(0.0, high_spread))
    return delta, max(-1.0, low), min(1.0, high)


@dataclass(frozen=True, slots=True)
class Comparison:
    """A candidate against its baseline on the same scenes."""

    scenes: int
    candidate_successes: int
    baseline_successes: int
    candidate_rate: float
    candidate_interval: tuple[float, float]
    baseline_rate: float
    baseline_interval: tuple[float, float]
    difference: float
    difference_interval: tuple[float, float]
    # Scenes only the candidate solved, and only the baseline solved.
    gained: int
    lost: int
    margin: float
    verdict: Verdict

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, float):
                data[key] = round(value, 4)
            elif isinstance(value, tuple):
                data[key] = [round(v, 4) for v in value]
        data["verdict"] = self.verdict.value
        data["confidence"] = "one-sided 95% bounds"
        return data


def compare(
    pairs: Sequence[tuple[bool, bool]], *, margin: float, z: float = Z_95_ONE_SIDED
) -> Comparison:
    """The paired comparison and its verdict. ``pairs``: (candidate succeeded, baseline
    succeeded) per scene."""
    if not 0 <= margin < 1:
        raise ValueError("the margin is a difference in success rate, from 0 to 1")
    n = len(pairs)
    cs = sum(1 for c, _ in pairs if c)
    bs = sum(1 for _, b in pairs if b)
    delta, low, high = difference_paired(pairs, z)
    if low > 0:
        verdict = Verdict.SUPERIOR
    elif low > -margin:
        verdict = Verdict.NON_INFERIOR
    elif high < -margin:
        verdict = Verdict.INFERIOR
    else:
        verdict = Verdict.INCONCLUSIVE
    return Comparison(
        scenes=n,
        candidate_successes=cs,
        baseline_successes=bs,
        candidate_rate=cs / n if n else 0.0,
        candidate_interval=wilson(cs, n, z),
        baseline_rate=bs / n if n else 0.0,
        baseline_interval=wilson(bs, n, z),
        difference=delta,
        difference_interval=(low, high),
        gained=sum(1 for c, b in pairs if c and not b),
        lost=sum(1 for c, b in pairs if b and not c),
        margin=margin,
        verdict=verdict,
    )


def passes(verdict: Verdict | str) -> bool:
    """Whether a verdict lets a release go ahead."""
    return Verdict(verdict) in (Verdict.SUPERIOR, Verdict.NON_INFERIOR)
