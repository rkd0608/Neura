"""The qualification protocol's defaults (design §7.1): what ``neura-qualify`` runs with unless
told otherwise, and the limits ``promote`` holds a run to. A run may be stricter, never looser.
"""

from __future__ import annotations

HOLD_TOLERANCE = 1.0  # degrees (0-100 units for the gripper): how still a held arm must stay
NOMINAL_S = 20.0  # the nominal run: control rate, loop timing, parity samples
PARITY_ATOL = 1e-3  # the largest open-loop difference from LeRobot, in the artifact's units
PAIRS = 30  # closed-loop episodes per arm
MARGIN = 0.15  # the non-inferiority margin on the closed-loop success rate
