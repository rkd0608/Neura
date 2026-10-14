"""Explicit unit conversions an execution profile may declare (design §4.3, §4.4).

Only linear conversions between units of the same physical dimension are allowed. Anything else
(for example absolute positions to deltas, or one frame to another) is not a unit conversion and
can never be declared as one.
"""

from __future__ import annotations

import math

from neura_contracts.core import Unit

_FACTORS: dict[tuple[Unit, Unit], float] = {
    (Unit.DEG, Unit.RAD): math.pi / 180.0,
    (Unit.RAD, Unit.DEG): 180.0 / math.pi,
    (Unit.DEG_PER_S, Unit.RAD_PER_S): math.pi / 180.0,
    (Unit.RAD_PER_S, Unit.DEG_PER_S): 180.0 / math.pi,
    (Unit.MM, Unit.M): 1e-3,
    (Unit.M, Unit.MM): 1e3,
}

SUPPORTED_CONVERSIONS: frozenset[tuple[Unit, Unit]] = frozenset(_FACTORS)


def conversion_factor(from_unit: Unit, to_unit: Unit) -> float | None:
    """Multiplicative factor converting ``from_unit`` to ``to_unit``, or None if unsupported."""
    return _FACTORS.get((from_unit, to_unit))
