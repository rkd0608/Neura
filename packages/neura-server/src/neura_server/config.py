"""Gateway configuration file (YAML) and the objects built from it."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from neura_contracts import ExecutionProfile, ModelArtifact, RobotProfile, load_document
from neura_runtime.binding import ExecutionBinding
from neura_server.profiles import ProfilePoint
from neura_server.scheduler import Policy

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DeviceConfig(_Model):
    robot_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    # Only the SHA-256 of the device token is stored; tokens never appear in config.
    token_sha256: Sha256Hex


class DeploymentConfig(_Model):
    revision_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    artifact: Path
    robot_profile: Path
    execution_profile: Path
    robots: list[str] = Field(min_length=1)


class ProfilePointConfig(_Model):
    batch_size: int = Field(ge=1)
    p50_ms: float = Field(gt=0)
    p99_ms: float = Field(gt=0)
    memory_mib: int = Field(default=0, ge=0)


class ServingConfig(_Model):
    backend: Literal["mock"] = "mock"
    # The mock backend sleeps for a latency sampled from the profile, emulating a GPU.
    simulate_latency: bool = True
    policy: Policy = Policy.DEADLINE_FAIRNESS
    max_batch: int = Field(default=8, ge=1, le=256)
    reserve_fraction: float = Field(default=0.2, ge=0, lt=1)
    memory_available_mib: int | None = Field(default=None, ge=1)
    profile: list[ProfilePointConfig] = Field(
        default_factory=lambda: [
            ProfilePointConfig(batch_size=1, p50_ms=18, p99_ms=25, memory_mib=900),
            ProfilePointConfig(batch_size=8, p50_ms=32, p99_ms=45, memory_mib=1400),
        ]
    )

    def profile_points(self) -> list[ProfilePoint]:
        return [
            ProfilePoint(p.batch_size, int(p.p50_ms * 1000), int(p.p99_ms * 1000), p.memory_mib)
            for p in self.profile
        ]


class GatewayConfig(_Model):
    listen: str = "127.0.0.1:50051"
    status_listen: str | None = "127.0.0.1:8091"
    lease_ttl_ms: int = Field(default=2000, ge=100)
    lease_store: Path | None = None  # None: in memory (development only)
    max_message_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    default_rtt_ms: float = Field(default=20, gt=0)
    devices: list[DeviceConfig] = Field(min_length=1)
    deployments: list[DeploymentConfig] = Field(min_length=1)
    serving: ServingConfig = Field(default_factory=ServingConfig)

    @classmethod
    def load(cls, path: Path) -> GatewayConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        config = cls.model_validate(data)
        # Contract paths are relative to the config file.
        base = path.parent
        deployments = [
            d.model_copy(
                update={
                    "artifact": (base / d.artifact).resolve(),
                    "robot_profile": (base / d.robot_profile).resolve(),
                    "execution_profile": (base / d.execution_profile).resolve(),
                }
            )
            for d in config.deployments
        ]
        lease_store = (base / config.lease_store).resolve() if config.lease_store else None
        return config.model_copy(update={"deployments": deployments, "lease_store": lease_store})


def load_binding(deployment: DeploymentConfig) -> ExecutionBinding:
    artifact = load_document(deployment.artifact.read_text(encoding="utf-8"))
    robot = load_document(deployment.robot_profile.read_text(encoding="utf-8"))
    profile = load_document(deployment.execution_profile.read_text(encoding="utf-8"))
    if not (
        isinstance(artifact, ModelArtifact)
        and isinstance(robot, RobotProfile)
        and isinstance(profile, ExecutionProfile)
    ):
        raise ValueError(f"deployment {deployment.revision_id}: documents have the wrong kinds")
    return ExecutionBinding(artifact, robot, profile)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DeviceRegistry:
    def __init__(self, devices: list[DeviceConfig]) -> None:
        self._by_digest = {d.token_sha256: d.robot_id for d in devices}
        if len(self._by_digest) != len(devices):
            raise ValueError("device tokens must be unique")

    def authenticate(self, token: str) -> str | None:
        digest = token_digest(token)
        for known, robot_id in self._by_digest.items():
            if hmac.compare_digest(known, digest):
                return robot_id
        return None
