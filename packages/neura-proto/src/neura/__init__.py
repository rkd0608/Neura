"""Neura. ``neura.v1`` holds the protocol's generated bindings; ``neura.policy`` declares a
class as a policy (``neura_contracts.authoring``)."""

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "policy":
        from neura_contracts.authoring import policy

        return policy
    raise AttributeError(f"module 'neura' has no attribute {name!r}")
