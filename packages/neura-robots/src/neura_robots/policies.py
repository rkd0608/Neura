"""The policy a robot runs without a site gateway: ``policy:`` in a ``neura-arm`` configuration,
opened and checked against the robot's contracts before anything can move (design §30.2).

    policy:
      uri: picker.py                  # an @neura.policy class, in this process
      uri: neura://gpu-box:50061      # the same, behind `neura-robot serve` (NEURA_SERVE_TOKEN)
      uri: openpi://gpu-box:8000      # openpi's policy server; `openpi:` maps the inputs
      uri: lerobot://gpu-box:8080     # LeRobot's async server; `lerobot:` names the checkpoint

Every one is driven by the same runtime: freshness, task epochs, merging, faults and the
operator's authority do not depend on where the policy runs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from neura_runtime.binding import ExecutionBinding
from neura_runtime.policy import PolicyClient, PolicyError


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OpenPIStateEntry(_Model):
    key: str  # the server's name, e.g. observation/state
    state: str = "observation.state"  # the state input it is taken from
    start: int = 0
    stop: int | None = None


class OpenPIConfig(_Model):
    state: list[OpenPIStateEntry]
    images: dict[str, str]  # the server's name -> camera role
    prompt: str | None = "prompt"
    images_under: str | None = None
    channels_first: bool = False
    api_key_env: str | None = None  # the environment variable holding the server's API key


class LeRobotConfig(_Model):
    policy_type: Literal["act", "smolvla", "diffusion", "tdmpc", "vqbet", "pi0", "pi05", "groot"]
    checkpoint: str  # a path or Hugging Face Hub name, as the server sees it
    device: str = "cpu"


class PolicyConfig(_Model):
    uri: str
    openpi: OpenPIConfig | None = None
    lerobot: LeRobotConfig | None = None
    weights: Path | None = None  # an @neura.policy class's weights, if not where it declares
    device: str = "cpu"  # where an @neura.policy class in this process runs
    timeout_s: float = Field(default=2.0, gt=0)

    @model_validator(mode="after")
    def _settings_match(self) -> PolicyConfig:
        scheme = self.scheme
        if scheme == "openpi" and self.openpi is None:
            raise ValueError("an openpi:// policy needs `openpi:` (how its inputs are named)")
        if scheme == "lerobot" and self.lerobot is None:
            raise ValueError(
                "a lerobot:// policy needs `lerobot:` (its policy type and checkpoint)"
            )
        return self

    @property
    def scheme(self) -> Literal["file", "neura", "openpi", "lerobot"]:
        head, sep, _ = self.uri.partition("://")
        if not sep:
            return "file"
        if head not in ("neura", "openpi", "lerobot"):
            raise ValueError(f"{self.uri}: use a .py file, neura://, openpi:// or lerobot://")
        return head  # type: ignore[return-value]

    @property
    def address(self) -> str:
        return self.uri.partition("://")[2]


def file_source(config: PolicyConfig, base: Path) -> str:
    """An @neura.policy file's ``path.py[:Class]``, its path resolved from ``base``."""
    path_text, sep, name = config.uri.partition(":")
    return str((base / path_text).resolve()) + (sep + name if sep else "")


def inferred_binding(config: PolicyConfig, robot: dict[str, Any], base: Path) -> ExecutionBinding:
    """The contracts for running an @neura.policy file on ``robot`` (a robot profile document),
    for a robot that describes itself (a simulated arm): inferred from what the policy declares
    (``neura_contracts.inference``), with limits from how fast it runs here (its slowest pass on
    made-up observations), as a site infers them. Other policies need ``revision:``."""
    from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, parse_document
    from neura_contracts.inference import (
        InferenceError,
        Timing,
        declared_artifact,
        derive_execution_profile,
        weights_manifest,
    )
    from neura_runtime.authored import declaration, slowest_pass_us

    if config.scheme != "file":
        raise PolicyError(f"{config.uri}: give `revision:` (the contracts) for a remote policy")
    source = file_source(config, base)
    declared = declaration(source)
    code = Path(source.partition(":")[0])
    files = weights_manifest(code.parent)
    files = [f for f in files if f["path"] == code.name]  # the policy's code
    if config.weights is not None:
        files += weights_manifest((base / config.weights).resolve())
    try:
        model = declared_artifact(
            declared,
            robot,
            name=code.stem.replace("_", "-"),
            version="sha-" + files[0]["digest"].removeprefix("sha256:")[:12],
            weights=files,
        )
        measured = slowest_pass_us(source, robot, weights=config.weights, device=config.device)
        timing = Timing({1: measured})
        profile, _ = derive_execution_profile(
            model, robot, timing, device=config.device, precision="fp32"
        )
    except InferenceError as error:
        raise PolicyError(f"{config.uri} cannot drive this robot: {error}") from error
    a, r, p = (parse_document(d) for d in (model, robot, profile))
    assert isinstance(a, ModelArtifact)
    assert isinstance(r, RobotProfile)
    assert isinstance(p, ExecutionProfile)
    return ExecutionBinding(a, r, p)


async def open_policy(config: PolicyConfig, binding: ExecutionBinding, base: Path) -> PolicyClient:
    """The policy, connected and checked against ``binding``; PolicyError if it cannot serve this
    robot. ``base`` is the directory a relative file is read from."""
    from neura_runtime.authored import AuthoredPolicy
    from neura_runtime.policy import InProcessPolicy

    scheme = config.scheme
    if scheme == "file":
        source = file_source(config, base)
        authored = AuthoredPolicy.load(source, weights=config.weights, device=config.device)
        if authored.declaration.chunk_length != binding.chunk_length:
            raise PolicyError(
                f"{config.uri} makes chunks of {authored.declaration.chunk_length} actions; the "
                f"robot's contracts expect {binding.chunk_length}"
            )
        return InProcessPolicy(authored)
    if scheme == "neura":
        from neura_runtime.remote import RemotePolicy

        remote = RemotePolicy(
            config.address,
            token=os.environ.get("NEURA_SERVE_TOKEN", ""),
            timeout_s=config.timeout_s,
        )
        await remote.check(binding)
        return remote
    if scheme == "openpi":
        from neura_runtime.openpi import OpenPIKeys, OpenPIPolicy, StateKey

        settings = config.openpi
        assert settings is not None  # the validator requires it
        keys = OpenPIKeys(
            state=[StateKey(s.key, s.state, s.start, s.stop) for s in settings.state],
            images=dict(settings.images),
            prompt=settings.prompt,
            images_under=settings.images_under,
            channels_first=settings.channels_first,
        )
        key = os.environ.get(settings.api_key_env, "") if settings.api_key_env else ""
        openpi = OpenPIPolicy(f"ws://{config.address}", keys, api_key=key)
        await openpi.connect()
        return openpi
    from neura_runtime.lerobot_server import LeRobotPolicy

    lerobot = config.lerobot
    assert lerobot is not None  # the validator requires it
    served = LeRobotPolicy(
        config.address,
        binding,
        policy_type=lerobot.policy_type,
        checkpoint=lerobot.checkpoint,
        device=lerobot.device,
        timeout_s=config.timeout_s,
    )
    await served.load()
    return served


def describe(config: PolicyConfig) -> str:
    return {
        "file": f"{config.uri}, in this process",
        "neura": f"the policy served at {config.address}",
        "openpi": f"openpi's server at {config.address}",
        "lerobot": f"LeRobot's server at {config.address}",
    }[config.scheme]
