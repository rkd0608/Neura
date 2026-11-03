"""LeRobot 0.6.1 on the transformers this workspace pins: one call LeRobot makes that it no longer
takes.

LeRobot 0.6.1 caps transformers below 5.6; the workspace overrides that with 5.10 for its security
fixes (pyproject.toml). transformers 5.10 dropped ``cache_position`` from ``create_causal_mask``
(positions come from ``position_ids``), and LeRobot's Gemma for pi0 and pi0.5
(``lerobot.policies.pi_gemma``) still passes it, so those policies fail on their first forward
pass. LeRobot derives ``position_ids`` from ``cache_position`` before the call, so dropping the
argument computes the same mask. Remove this module when a LeRobot release allows transformers
5.10.
"""

from __future__ import annotations

import inspect
from typing import Any


def policy_class(policy_type: str) -> Any:
    """LeRobot's policy class for ``policy_type`` (``get_policy_class``), fitted to the pinned
    transformers. Every LeRobot policy Neura loads comes through here."""
    from lerobot.policies.factory import get_policy_class

    _fit_pi_gemma()
    return get_policy_class(policy_type)


def _fit_pi_gemma() -> None:
    from lerobot.policies import pi_gemma
    from transformers import masking_utils

    original = masking_utils.create_causal_mask
    if pi_gemma.create_causal_mask is not original:
        return  # already fitted, or not the function this was written for
    if "cache_position" in inspect.signature(original).parameters:
        return  # a transformers that still takes it

    def create_causal_mask(*args: Any, cache_position: Any = None, **kwargs: Any) -> Any:
        del cache_position  # position_ids carries it (pi_gemma.PiGemmaModel.forward)
        return original(*args, **kwargs)

    pi_gemma.create_causal_mask = create_causal_mask
