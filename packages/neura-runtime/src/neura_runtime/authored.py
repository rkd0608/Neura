"""Running an ``@neura.policy`` class: the observation it reads and the chunk it must return
(design §29.3, §30.2).

The class sees LeRobot's names: ``observation.state`` (and any other state input by its name),
``observation.images.<role>`` as uint8 (height, width, 3) in the camera's declared color order,
and ``task`` when the policy reads language. It returns a (chunk length, action features) array
of finite numbers in the robot's command order and units. The same rules hold wherever the class
runs: in a site's policy worker (the ``python`` adapter), in a robot's process, or behind
``neura-robot serve``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_contracts import authoring
from neura_contracts.inference import PolicyDeclaration
from neura_runtime.policy import ModelInputs, PolicyError


def observation(inputs: ModelInputs) -> dict[str, Any]:
    """What ``act`` reads, from one observation's validated inputs."""
    out: dict[str, Any] = dict(inputs.state)
    for role, image in inputs.images.items():
        out[f"observation.images.{role}"] = image
    if inputs.instruction:
        out["task"] = inputs.instruction
    return out


def checked(result: Any, expected: tuple[int, int] | None = None) -> NDArray[np.float32]:
    """``act``'s result as a float32 chunk, or PolicyError when it is not one."""
    chunk = np.asarray(result, dtype=np.float32)
    if chunk.ndim != 2 or (expected is not None and chunk.shape != expected):
        wanted = f"{expected}" if expected is not None else "a 2-D array"
        raise PolicyError(
            f"the policy returned {tuple(chunk.shape)}; the robot needs {wanted} "
            "(chunk_length, action features)"
        )
    if not np.isfinite(chunk).all():
        raise PolicyError("the policy returned NaN or infinite actions")
    return chunk


class AuthoredPolicy:
    """One ``@neura.policy`` class, loaded and called one observation at a time."""

    def __init__(self, instance: Any, declaration: PolicyDeclaration) -> None:
        self.instance = instance
        self.declaration = declaration

    @classmethod
    def load(
        cls, source: str, *, weights: Path | None = None, device: str = "cpu"
    ) -> AuthoredPolicy:
        """``source`` is ``path/to/file.py`` (its one ``@neura.policy`` class) or
        ``path/to/file.py:Class``. ``weights`` defaults to the class's declared ``weights``,
        relative to the file. Runs the file's code."""
        path, policy_class, marked = _policy_class(source)
        if weights is None:
            weights = path.parent / marked.weights if marked.weights else path.parent
        instance = authoring.instantiate(policy_class, weights=weights, device=device)
        return cls(instance, marked.declaration)

    def __call__(self, inputs: ModelInputs) -> NDArray[np.float32]:
        try:
            result = self.instance.act(observation(inputs))
        except MemoryError as error:
            raise PolicyError(
                f"the policy ran out of memory: {error}", out_of_memory=True
            ) from error
        except PolicyError:
            raise
        except Exception as error:  # the policy's own code: reported to the robot as a reason
            raise PolicyError(f"the policy failed: {type(error).__name__}: {error}") from error
        return checked(result)


def synthetic_observation(
    declaration: PolicyDeclaration, robot: Mapping[str, Any], *, seed: int = 0
) -> dict[str, Any]:
    """An observation shaped as the robot sends it: state in its order, images at the sizes the
    policy reads, and a task when it reads language."""
    rng = np.random.default_rng(seed)
    spec = robot["spec"]
    observation: dict[str, Any] = {}
    if declaration.state:
        features = spec["state_features"]
        observation["observation.state"] = rng.uniform(-1, 1, len(features)).astype(np.float32)
    cameras = declaration.cameras
    if cameras is None:
        cameras = {c["role"]: (int(c["width"]), int(c["height"])) for c in spec.get("cameras", [])}
    for role, (width, height) in cameras.items():
        observation[f"observation.images.{role}"] = rng.integers(
            0, 256, (height, width, 3), dtype=np.uint8
        )
    if declaration.language:
        observation["task"] = "pick up the cube"
    return observation


def slowest_pass_us(
    source: str,
    robot: Mapping[str, Any],
    *,
    weights: Path | None = None,
    device: str = "cpu",
    runs: int = 5,
) -> int:
    """The slowest of ``runs`` calls of a fresh instance of the class at ``source`` on made-up
    observations (``synthetic_observation``), in µs: how fast it runs on this machine. The
    instance is thrown away, so a stateful policy serves from a clean start."""
    policy = AuthoredPolicy.load(source, weights=weights, device=device)
    slowest = 0.0
    for seed in range(runs):
        observation = synthetic_observation(policy.declaration, robot, seed=seed)
        started = time.perf_counter()
        policy.instance.act(observation)
        slowest = max(slowest, time.perf_counter() - started)
    return max(1, round(slowest * 1_000_000))


def declaration(source: str) -> PolicyDeclaration:
    """What the ``@neura.policy`` class at ``source`` declares, without instantiating it (its
    module's code runs)."""
    return _policy_class(source)[2].declaration


def _policy_class(source: str) -> tuple[Path, type, authoring.Authored]:
    path_text, _, class_name = source.partition(":")
    path = Path(path_text).resolve()
    if path.suffix != ".py" or not path.is_file():
        raise authoring.AuthoringError(f"{path_text} is not a Python file")
    if not class_name:
        class_name = _only_policy_class(path)
    policy_class = authoring.load_class(path.parent, f"{path.stem}:{class_name}")
    marked = authoring.authored(policy_class)
    assert marked is not None  # load_class refuses a class without the decorator
    return path, policy_class, marked


def _only_policy_class(path: Path) -> str:
    """The name of the one ``@neura.policy`` class ``path`` defines, found without running it."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    names = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any("policy" in ast.unparse(d) for d in node.decorator_list)
    ]
    if len(names) != 1:
        raise authoring.AuthoringError(
            f"{path.name} defines {len(names)} @neura.policy classes: name one ({path.name}:Class)"
        )
    return names[0]
