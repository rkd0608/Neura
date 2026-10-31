"""The release statistics: intervals that behave at 0% and 100%, and verdicts that need the
evidence to decide."""

from __future__ import annotations

import pytest

from neura_contracts.stats import (
    Verdict,
    compare,
    difference_paired,
    difference_unpaired,
    passes,
    wilson,
)


def test_wilson_and_newcombe_match_the_published_example() -> None:
    # Newcombe (1998), Statistics in Medicine 17:873, example (a): 56/70 vs 48/80, method 10.
    z = 1.959963984540054
    delta, low, high = difference_unpaired(56, 70, 48, 80, z)
    assert (delta, low, high) == pytest.approx((0.2, 0.0524, 0.3339), abs=1e-4)
    assert wilson(56, 70, z) == pytest.approx((0.6918, 0.8770), abs=1e-4)  # closed form


def test_wilson_intervals_stay_inside_zero_and_one() -> None:
    low, high = wilson(0, 10)
    assert low == 0.0
    assert 0.15 < high < 0.25
    low, high = wilson(10, 10)
    assert high == 1.0
    assert 0.75 < low < 0.85
    assert wilson(0, 0) == (0.0, 1.0)
    low, high = wilson(33, 50)
    assert low < 0.66 < high


def test_paired_intervals_contain_the_estimate_and_use_the_pairing() -> None:
    # Two models that agree on most scenes: pairing narrows the interval.
    pairs = [(True, True)] * 30 + [(False, False)] * 14 + [(True, False)] * 4 + [(False, True)] * 2
    delta, low, high = difference_paired(pairs)
    assert delta == pytest.approx(0.04)
    assert low < delta < high
    _, unpaired_low, unpaired_high = difference_unpaired(34, 50, 32, 50)
    assert high - low < unpaired_high - unpaired_low
    assert difference_paired([]) == (0.0, -1.0, 1.0)


@pytest.mark.parametrize(
    ("pairs", "verdict"),
    [
        ([(True, True)] * 50, Verdict.NON_INFERIOR),  # the same, with enough scenes
        ([(True, False)] * 25 + [(True, True)] * 25, Verdict.SUPERIOR),
        ([(False, True)] * 35 + [(True, True)] * 10 + [(False, False)] * 5, Verdict.INFERIOR),
        ([(True, True)] * 3 + [(False, True)] * 1 + [(True, False)] * 1, Verdict.INCONCLUSIVE),
    ],
)
def test_verdicts(pairs: list[tuple[bool, bool]], verdict: Verdict) -> None:
    comparison = compare(pairs, margin=0.10)
    assert comparison.verdict is verdict
    assert comparison.scenes == len(pairs)
    assert passes(verdict) is (verdict in (Verdict.SUPERIOR, Verdict.NON_INFERIOR))
    data = comparison.to_json()
    assert data["verdict"] == verdict.value
    assert data["gained"] + data["lost"] <= data["scenes"]


def test_the_margin_is_a_rate_difference() -> None:
    with pytest.raises(ValueError, match="margin"):
        compare([(True, True)], margin=1.5)
