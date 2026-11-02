"""The with/without comparison (``neura_robots.compare``): under latency spikes the plain loop
sends the arm commands planned for moments long gone, and the same policy through neura-robot's
runtime sends none.
"""

from __future__ import annotations

import pytest

pytest.importorskip("neura_robots.sim")  # MuJoCo's OpenGL backend, or what is missing
pytest.importorskip("mujoco")

from neura_robots.compare import loops, metrics, scenarios


def test_under_latency_spikes_only_the_plain_loop_acts_on_the_past() -> None:
    spikes = next(c for c in scenarios.CONDITIONS if c.name == "spikes")
    without = metrics.measure(loops.naive(spikes))
    with_neura = metrics.measure(loops.neura(spikes))
    assert without.late_commands > 0
    assert with_neura.late_commands == 0
    assert with_neura.commands > without.commands  # neura did not get there by sending less
