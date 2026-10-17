"""Site-agent configuration, read from ``NEURA_AGENT_*`` environment variables.

Boundaries live here, on the server, not in the cloud (design §17.4): which operations the cloud may
request, which GPUs Neura may use, and who may approve activation locally. A compromised or mistaken
control plane cannot widen them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal, get_args

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RemoteOperation = Literal[
    "prepare", "request_activation", "rollback", "collect_diagnostics", "cancel", "upgrade_agent"
]
# Remote agent upgrades run new code on the server: the site operator opts in to them.
DEFAULT_OPERATIONS = [o for o in get_args(RemoteOperation) if o != "upgrade_agent"]


class ArtifactKey(BaseModel):
    """A customer-managed key that opens sealed weights (design §18.2): a key-encryption key in a
    file on this server, or a KMS reached through a command (``<command> wrap|unwrap``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: Path | None = None
    command: list[str] | None = None
    key_id: str | None = None  # required with command; a file key's id derives from the key

    @model_validator(mode="after")
    def _one_kind(self) -> ArtifactKey:
        if (self.file is None) == (self.command is None):
            raise ValueError("an artifact key has either file or command")
        if self.command is not None and not self.key_id:
            raise ValueError("a command key needs its key_id")
        return self


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEURA_AGENT_", frozen=True, extra="ignore")

    state_dir: Path = Path("/var/lib/neura-agent")
    # Development stacks and tests only. Every setting below marked DEVELOPMENT ONLY, and every
    # insecure choice (plaintext to the control plane, robots without TLS, the simulated policy,
    # workers outside the sandbox, unsigned artifacts), is refused unless this is set.
    development: bool = False
    # Used before enrollment for the reachability probe; afterwards the enrolled endpoint is used.
    control_plane_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8080")
    # The local site API binds to loopback by default; operators expose it deliberately.
    local_host: str = "127.0.0.1"
    local_port: int = Field(default=8090, ge=1, le=65535)
    probe_interval_s: float = Field(default=5.0, gt=0, le=300)
    probe_timeout_s: float = Field(default=2.0, gt=0, le=60)
    status_interval_s: float = Field(default=2.0, gt=0, le=60)
    reconnect_max_backoff_s: float = Field(default=10.0, gt=0)
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    # The agent's channel to the control plane uses TLS, verified against the system roots or
    # this CA bundle. control_plane_insecure disables TLS, for local development only.
    control_plane_ca_file: Path | None = None
    control_plane_insecure: bool = False

    # --- boundaries (design §17.4) ---
    allowed_operations: list[RemoteOperation] = Field(
        default_factory=lambda: list(DEFAULT_OPERATIONS)
    )
    allowed_gpu_indexes: list[int] | None = None  # None: every GPU the server reports
    # SHA-256 of the local operator credential. Without it, local approvals are disabled.
    operator_token_sha256: str | None = None
    activation_timeout_s: float = Field(default=600.0, gt=0)

    # --- the data plane this agent supervises ---
    gateway_listen: str = "0.0.0.0:50051"
    gateway_status_listen: str | None = "127.0.0.1:8091"
    # Mutual TLS between robots and the gateway, with certificates from the site CA. Disabling it
    # (robots then use device tokens over plaintext) is for development only.
    gateway_tls: bool = True
    # Names and addresses robots use to reach the gateway, for its certificate. The host name,
    # localhost and 127.0.0.1 are always included.
    gateway_tls_names: list[str] = Field(default_factory=list)
    gateway_command: list[str] = Field(
        default_factory=lambda: [sys.executable, "-m", "neura_server"]
    )
    # workers: real policy worker processes running the artifact's adapter, with weights from
    # the artifact store (production). simulated: the mock policy, for development stacks.
    gateway_backend: Literal["workers", "simulated"] = "workers"
    gateway_simulate_latency: bool = True
    gateway_max_batch: int = Field(default=8, ge=1, le=256)
    worker_command: list[str] = Field(
        default_factory=lambda: [sys.executable, "-m", "neura_server.worker"]
    )
    # How policy workers run (design §12: customer model code is untrusted). container: the
    # artifact's runtime image, pinned by digest, with no network, a read-only root, no
    # capabilities and resource limits. process: a plain child process (development only).
    worker_sandbox: Literal["container", "process"] = "container"
    worker_container_runtime: list[str] = Field(default_factory=lambda: ["docker"])
    worker_memory_mib: int | None = Field(default=None, ge=256)
    worker_cpus: float | None = Field(default=None, gt=0)
    worker_pids_limit: int = Field(default=512, ge=16)
    worker_image_pull_timeout_s: float = Field(default=1800.0, gt=0)
    # DEVELOPMENT ONLY: run workers in this image (a digest or name@digest) instead of the
    # artifact's runtime image.
    worker_image_override: str | None = None
    # Profiling at prepare: forward passes timed per batch size, and how long loading and
    # profiling a model may take.
    profile_iterations: int = Field(default=30, ge=5, le=1000)
    profile_timeout_s: float = Field(default=1800.0, gt=0)

    # --- model weights (design §18.1: they stay with the customer) ---
    # Where the agent fetches weight files it does not have: directories or HTTPS base URLs laid
    # out as <source>/sha256/<hex>. Empty: only weights imported with `neura-agent import-weights`.
    artifact_sources: list[str] = Field(default_factory=list)
    artifact_source_token: SecretStr | None = None
    # Keys that open sealed weights from those sources, e.g.
    # [{"file": "/etc/neura/weights.kek"}] or [{"key_id": "aws-kms:alias/neura", "command": [...]}].
    artifact_keys: list[ArtifactKey] = Field(default_factory=list)
    # Publisher keys (Ed25519, hex or PEM) whose signature an artifact needs before this server
    # pulls its weights or runs it (design §17.3, §17.4): inline, and one per line in a trust
    # file (default <state_dir>/trusted-publishers, edited with `neura-agent trust-publisher`).
    trusted_publisher_keys: list[str] = Field(default_factory=list)
    trusted_publisher_keys_file: Path | None = None
    # DEVELOPMENT ONLY when false: run artifacts whatever their signatures.
    require_artifact_signatures: bool = True
    # Round trip to robots assumed by capacity estimates until robots report measured ones.
    capacity_rtt_p99_ms: float = Field(default=20.0, gt=0)

    # --- installation and upgrades (design §17.3) ---
    # Where the installer put the agent's releases, and the Neura release keys every release
    # must be signed with.
    install_dir: Path = Path("/opt/neura/agent")
    release_keys_file: Path = Path("/etc/neura/release-keys.pem")
    # How long an upgrade waits for every robot to be between tasks, and how long a new release
    # has to reconnect to the control plane before it is rolled back.
    upgrade_wait_s: float = Field(default=3600.0, gt=0)
    upgrade_health_timeout_s: float = Field(default=300.0, gt=0)

    # --- recorded episodes (design §28.2: they stay at the site) ---
    # The site operator's boundary on recording: what a deployment may ask robots to record.
    # off (the default): nothing, whatever deployments ask; failures: only episodes that end in
    # a fault, a takeover or a failure; all: every episode.
    capture_allowed: Literal["off", "failures", "all"] = "off"
    # The capture service robots upload finished episodes to, and the address they are told to
    # use (":port" for the host they reach the gateway on).
    capture_listen: str = "0.0.0.0:50052"
    capture_endpoint: str = ":50052"
    # Where episodes are kept (default <state_dir>/capture), and the most they may use; beyond
    # either limit the oldest episodes no dataset holds are deleted.
    capture_dir: Path | None = None
    capture_max_gib: float = Field(default=200.0, gt=0)
    capture_max_age_days: int | None = Field(default=None, ge=1)

    # --- what leaves the site (design §18.2) ---
    # standard: every field of the published outbound schema (catalog/outbound.yaml). minimal:
    # only health, versions, identifiers, states and reason codes; metrics stay at the site.
    telemetry_mode: Literal["standard", "minimal"] = "standard"
    # The outbound ledger (<state_dir>/outbound) keeps at most this much, newest first.
    outbound_ledger_max_mib: int = Field(default=256, ge=32, le=1 << 20)

    # --- preflight ---
    require_gpu: bool = False
    min_free_disk_mib: int = Field(default=1024, ge=0)

    # DEVELOPMENT ONLY: run every revision's worker with this adapter (e.g. "mock") instead of
    # the artifact's, so real worker processes run without the artifact's weights.
    dev_adapter_override: str | None = None
    # DEVELOPMENT ONLY: issue every robot of the site a key and certificate into this directory
    # (`<robot>.key`, `<robot>.pem`, `ca.pem`), for simulated robots. Real robots make their own
    # keys and send a CSR.
    dev_robot_certificate_dir: Path | None = None
    # DEVELOPMENT ONLY: robot name -> plain device token, so a local stack works without issuing
    # credentials through the local API.
    dev_robot_tokens: dict[str, str] | None = None

    @model_validator(mode="after")
    def _insecure_only_in_development(self) -> AgentSettings:
        insecure = {
            "control_plane_insecure": self.control_plane_insecure,
            "gateway_tls=false": not self.gateway_tls,
            "gateway_backend=simulated": self.gateway_backend == "simulated",
            "worker_sandbox=process": (
                self.gateway_backend == "workers" and self.worker_sandbox == "process"
            ),
            "require_artifact_signatures=false": not self.require_artifact_signatures,
            "worker_image_override": self.worker_image_override is not None,
            "dev_adapter_override": self.dev_adapter_override is not None,
            "dev_robot_certificate_dir": self.dev_robot_certificate_dir is not None,
            "dev_robot_tokens": self.dev_robot_tokens is not None,
            "artifact_sources over plain http": any(
                source.startswith("http://") for source in self.artifact_sources
            ),
        }
        chosen = [name for name, on in insecure.items() if on]
        if chosen and not self.development:
            raise ValueError(
                f"{', '.join(chosen)}: development only; set NEURA_AGENT_DEVELOPMENT=true on a "
                "development machine"
            )
        return self
