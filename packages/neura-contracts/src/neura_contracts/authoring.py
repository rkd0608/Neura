"""``@neura.policy``: any model, in any framework, as a Neura policy (design §29.3).

A class that turns an observation into a chunk of actions is all Neura needs::

    import neura

    @neura.policy(chunk_length=50, cameras={"front": (640, 480)}, weights="checkpoint/")
    class Picker:
        def __init__(self, weights: Path, device: str) -> None:
            self.model = load_my_model(weights, device)

        def act(self, observation: dict[str, Any]) -> np.ndarray:
            # observation["observation.state"]: float32 (state components,), in the robot's
            #   order and units
            # observation["observation.images.front"]: uint8 (height, width, 3)
            # observation["task"]: str, when the policy reads language
            return self.model(observation)  # (chunk_length, action features)

``neura policy push picker.py`` reads the declaration, writes the model artifact for the robot,
and ships the class's directory (code and weights) to the site, where the ``python`` adapter
imports the class and calls it. ``act_batch(observations)`` (optional) serves several robots in
one call; ``reset()`` (optional) is called when a robot starts a new task. The class runs in the
site's policy worker sandbox, like every other policy.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from neura_contracts.inference import PolicyDeclaration

ATTRIBUTE = "__neura_policy__"
# The file a python or container version carries next to its code: how the site runs it.
ENTRY_FILE = "neura-policy.json"

T = TypeVar("T", bound=type)


@dataclass(frozen=True, slots=True)
class Authored:
    """What ``@neura.policy`` attaches to a class."""

    declaration: PolicyDeclaration
    # The weights directory, relative to the file that defines the class (None: no weights, or
    # they live next to the code).
    weights: str | None


class AuthoringError(TypeError):
    pass


def policy(
    *,
    chunk_length: int,
    cameras: Mapping[str, tuple[int, int]] | None = None,
    state: bool = True,
    language: bool = False,
    executed_actions: int | None = None,
    deterministic: bool = False,
    stateful: bool = False,
    batchable: bool | None = None,
    weights: str | None = None,
    description: str = "",
) -> Callable[[T], T]:
    """Declare a class as a Neura policy. ``cameras`` maps each camera role it reads to the
    (width, height) it wants; ``None`` reads every camera of the robot at its own size.
    ``batchable`` defaults to whether the class defines ``act_batch``."""

    def mark(cls: T) -> T:
        if not isinstance(cls, type):
            raise AuthoringError("@neura.policy decorates a class")
        if not callable(getattr(cls, "act", None)):
            raise AuthoringError(f"{cls.__name__} needs an act(observation) method")
        declaration = PolicyDeclaration(
            adapter="python",
            chunk_length=chunk_length,
            executed_actions=executed_actions,
            cameras=None
            if cameras is None
            else {r: (int(s[0]), int(s[1])) for r, s in cameras.items()},
            state=state,
            language=language,
            deterministic=deterministic,
            stateful=stateful,
            batchable=callable(getattr(cls, "act_batch", None)) if batchable is None else batchable,
            description=description or (cls.__doc__ or "").strip().split("\n", 1)[0],
        )
        setattr(cls, ATTRIBUTE, Authored(declaration, weights))
        return cls

    return mark


def authored(cls: Any) -> Authored | None:
    value = getattr(cls, ATTRIBUTE, None)
    return value if isinstance(value, Authored) else None


def entry_document(
    *,
    kind: str,
    declaration: PolicyDeclaration,
    entry: str | None = None,
    weights: str | None = None,
) -> dict[str, Any]:
    """The ``neura-policy.json`` of a python version (``entry``: ``module:Class``, relative to
    the version's files) or a container version."""
    document: dict[str, Any] = {"kind": kind, "declaration": declaration.to_json()}
    if entry is not None:
        document["entry"] = entry
    if weights is not None:
        document["weights"] = Path(weights).as_posix().rstrip("/")
    return document


def load_class(root: Path, entry: str) -> type:
    """The ``@neura.policy`` class ``entry`` (``module:Class``) from the files under ``root``.
    Runs the module's code: on a site, inside the policy worker's sandbox."""
    module_name, _, class_name = entry.partition(":")
    path = root / f"{module_name.replace('.', '/')}.py"
    unique = f"_neura_policy_{module_name.replace('.', '_')}_{abs(hash(str(root))):x}"
    spec = importlib.util.spec_from_file_location(unique, path)
    if spec is None or spec.loader is None:
        raise AuthoringError(f"{path} is not a Python file")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(root))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(root))
    cls = getattr(module, class_name, None)
    if not isinstance(cls, type) or authored(cls) is None:
        raise AuthoringError(f"{path} has no @neura.policy class {class_name}")
    return cls


def instantiate(cls: type, *, weights: Path, device: str) -> Any:
    """``cls(weights=..., device=...)``, passing only the arguments its constructor takes."""
    parameters = inspect.signature(cls).parameters
    offered = {"weights": weights, "device": device}
    return cls(**{k: v for k, v in offered.items() if k in parameters})
