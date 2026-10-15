"""Gateway configuration file (YAML) and the objects built from it."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from neura_contracts import (
    DeviceKind,
    ExecutionProfile,
    ModelArtifact,
    RobotProfile,
    load_document,
    pki,
)
from neura_runtime.binding import ExecutionBinding
from neura_server.profiles import ProfilePoint
from neura_server.scheduler import Policy

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DeviceConfig(_Model):
    robot_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    # With TLS: the SHA-256 fingerprints of the robot's client certificates the site CA issued
    # (several while one is being replaced). Without TLS (development only): the SHA-256 of a
    # device token; tokens never appear in config.
    certificate_sha256: list[Sha256Hex] = Field(default_factory=list)
    token_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def _credential(self) -> DeviceConfig:
        if not self.certificate_sha256 and self.token_sha256 is None:
            raise ValueError(f"device {self.robot_id} has no certificate or token")
        return self


class TlsConfig(_Model):
    """The gateway's TLS: its server certificate and key, and the CA whose client certificates
    robots must present (the site CA). Files are re-read when they change, so certificates
    rotate without a restart."""

    certificate: Path
    key: Path
    client_ca: Path


Device = Annotated[str, StringConstraints(pattern=r"^(cpu|cuda:[0-9]{1,3})$")]


class DeploymentConfig(_Model):
    revision_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    artifact: Path
    robot_profile: Path
    execution_profile: Path
    robots: list[str] = Field(min_length=1)
    # The rest matter to the workers backend only.
    # Directory holding the artifact's weight files, verified against their digests.
    checkpoint: Path | None = None
    # Overrides the artifact's adapter, e.g. "mock" to exercise real worker processes without
    # weights. Development and tests only.
    adapter: str | None = None
    # The device this deployment's worker runs on; None: the execution profile's device kind
    # (its first device).
    device: Device | None = None
    # Latency profile measured on that device (`neura-worker profile` output). None: the
    # serving section's configured profile.
    profile: Path | None = None


class ProfilePointConfig(_Model):
    batch_size: int = Field(ge=1)
    p50_ms: float = Field(gt=0)
    p99_ms: float = Field(gt=0)
    memory_mib: int = Field(default=0, ge=0)


class ServingConfig(_Model):
    # workers: one policy worker process per batch group, running the artifact's adapter on its
    # device (design §12). simulated: the mock policy in the gateway process, for development
    # stacks and tests.
    backend: Literal["workers", "simulated"] = "workers"
    # The simulated backend sleeps for a latency sampled from the profile, emulating a GPU.
    simulate_latency: bool = True
    worker_command: list[str] = Field(
        default_factory=lambda: [sys.executable, "-m", "neura_server.worker"], min_length=1
    )
    # Where worker sockets live; None: a private temporary directory.
    worker_dir: Path | None = None
    # Loading and warming up a large model can take minutes.
    worker_start_timeout_s: float = Field(default=600, gt=0)
    # A batch that takes longer than its latest deadline and at least this long marks the worker
    # hung: it is restarted and the batch fails with WORKER_TIMED_OUT.
    worker_timeout_ms: float = Field(default=1000, gt=0)
    policy: Policy = Policy.DEADLINE_FAIRNESS
    max_batch: int = Field(default=8, ge=1, le=256)
    # How long a queued request may wait for others to share its batch (within deadlines).
    batch_window_ms: float = Field(default=2.0, ge=0, le=50)
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
    # The site this gateway serves; robots' certificates must name it.
    site_id: str | None = None
    listen: str = "127.0.0.1:50051"
    # Mutual TLS with robots. None: plaintext with device tokens, for development only.
    tls: TlsConfig | None = None
    status_listen: str | None = "127.0.0.1:8091"
    lease_ttl_ms: int = Field(default=2000, ge=100)
    lease_store: Path | None = None  # None: in memory (development only)
    max_message_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    default_rtt_ms: float = Field(default=20, gt=0)
    devices: list[DeviceConfig] = Field(default_factory=list)
    deployments: list[DeploymentConfig] = Field(default_factory=list)
    # Robot -> the revision it should run (its next one during an activation).
    assignments: dict[str, str] = Field(default_factory=dict)
    serving: ServingConfig = Field(default_factory=ServingConfig)

    @model_validator(mode="after")
    def _credentials_match_transport(self) -> GatewayConfig:
        for device in self.devices:
            if self.tls is not None and not device.certificate_sha256:
                raise ValueError(f"device {device.robot_id} needs a certificate: TLS is on")
        return self

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
                    "checkpoint": (base / d.checkpoint).resolve() if d.checkpoint else None,
                    "profile": (base / d.profile).resolve() if d.profile else None,
                }
            )
            for d in config.deployments
        ]
        lease_store = (base / config.lease_store).resolve() if config.lease_store else None
        tls = config.tls
        if tls is not None:
            tls = TlsConfig(
                certificate=(base / tls.certificate).resolve(),
                key=(base / tls.key).resolve(),
                client_ca=(base / tls.client_ca).resolve(),
            )
        serving = config.serving
        if serving.worker_dir is not None:
            serving = serving.model_copy(
                update={"worker_dir": (base / serving.worker_dir).resolve()}
            )
        return config.model_copy(
            update={
                "deployments": deployments,
                "lease_store": lease_store,
                "serving": serving,
                "tls": tls,
            }
        )


def load_binding(deployment: DeploymentConfig) -> ExecutionBinding:
    try:
        return load_binding_files(
            deployment.artifact, deployment.robot_profile, deployment.execution_profile
        )
    except ValueError as exc:
        raise ValueError(f"deployment {deployment.revision_id}: {exc}") from exc


def load_binding_files(artifact: Path, robot: Path, profile: Path) -> ExecutionBinding:
    documents = [load_document(p.read_text(encoding="utf-8")) for p in (artifact, robot, profile)]
    model, robot_profile, execution = documents
    if not (
        isinstance(model, ModelArtifact)
        and isinstance(robot_profile, RobotProfile)
        and isinstance(execution, ExecutionProfile)
    ):
        raise ValueError("documents have the wrong kinds")
    return ExecutionBinding(model, robot_profile, execution)


def deployment_device(binding: ExecutionBinding, configured: str | None) -> str:
    """The concrete device a deployment runs on."""
    if configured is not None:
        return configured
    return "cuda:0" if binding.profile.spec.runtime.device is DeviceKind.CUDA else "cpu"


def profile_batch_sizes(max_batch: int) -> list[int]:
    """Batch sizes workers warm up and profilers measure: powers of two up to max_batch, and
    max_batch itself."""
    sizes = {1, max_batch}
    size = 2
    while size < max_batch:
        sizes.add(size)
        size *= 2
    return sorted(sizes)


def load_profile_points(path: Path) -> list[ProfilePoint]:
    """Points of a measured profile (neura_adapters.profiler.MeasuredProfile.to_json)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    points = [
        ProfilePoint(int(p["batch_size"]), int(p["p50_us"]), int(p["p99_us"]), int(p["memory_mib"]))
        for p in data["points"]
    ]
    if not points:
        raise ValueError(f"{path}: the measured profile has no points")
    return points


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DeviceRegistry:
    """Which robot a credential belongs to."""

    def __init__(self, devices: list[DeviceConfig], *, site_id: str | None = None) -> None:
        self._site_id = site_id
        self._by_digest = {d.token_sha256: d.robot_id for d in devices if d.token_sha256}
        if len(self._by_digest) != sum(1 for d in devices if d.token_sha256):
            raise ValueError("device tokens must be unique")
        self._by_fingerprint: dict[str, str] = {}
        for device in devices:
            for fingerprint in device.certificate_sha256:
                if fingerprint in self._by_fingerprint:
                    raise ValueError("a certificate can belong to one robot only")
                self._by_fingerprint[fingerprint] = device.robot_id

    def robots(self) -> set[str]:
        return {*self._by_digest.values(), *self._by_fingerprint.values()}

    def authenticate(self, token: str) -> str | None:
        digest = token_digest(token)
        for known, robot_id in self._by_digest.items():
            if hmac.compare_digest(known, digest):
                return robot_id
        return None

    def authenticate_certificate(self, certificate_pem: bytes) -> str | None:
        """The robot a verified client certificate belongs to: TLS checked that the site CA
        issued it and that it is valid now; it must also still be allowed (not revoked) and name
        this robot and site."""
        try:
            fingerprint = pki.fingerprint(certificate_pem)
            identity = pki.identity(certificate_pem)
        except pki.CertificateError:
            return None
        robot_id = self._by_fingerprint.get(fingerprint)
        if robot_id is None or identity is None:
            return None
        if self._site_id is not None and identity != pki.robot_uri(self._site_id, robot_id):
            return None
        if self._site_id is None and not identity.endswith(f"/{robot_id}"):
            return None
        return robot_id
