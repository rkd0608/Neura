"""Site-agent configuration, read from ``NEURA_AGENT_*`` environment variables.

Boundaries live here, on the server, not in the cloud (design §17.4): which operations the cloud may
request, which GPUs Neura may use, and who may approve activation locally. A compromised or mistaken
control plane cannot widen them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal, get_args

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

RemoteOperation = Literal[
    "prepare", "request_activation", "rollback", "collect_diagnostics", "cancel"
]


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEURA_AGENT_", frozen=True, extra="ignore")

    state_dir: Path = Path("/var/lib/neura-agent")
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
        default_factory=lambda: list(get_args(RemoteOperation))
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
    # Profiling at prepare: forward passes timed per batch size, and how long loading and
    # profiling a model may take.
    profile_iterations: int = Field(default=30, ge=5, le=1000)
    profile_timeout_s: float = Field(default=1800.0, gt=0)

    # --- model weights (design §18.1: they stay with the customer) ---
    # Where the agent fetches weight files it does not have: directories or HTTPS base URLs laid
    # out as <source>/sha256/<hex>. Empty: only weights imported with `neura-agent import-weights`.
    artifact_sources: list[str] = Field(default_factory=list)
    artifact_source_token: SecretStr | None = None
    # Round trip to robots assumed by capacity estimates until robots report measured ones.
    capacity_rtt_p99_ms: float = Field(default=20.0, gt=0)

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
