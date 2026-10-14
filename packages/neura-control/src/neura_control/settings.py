"""Control-plane configuration, read from ``NEURA_CONTROL_*`` environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_SPEC = Path(__file__).resolve().parents[4] / "api" / "openapi.yaml"


class ControlPlaneSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEURA_CONTROL_", frozen=True, extra="ignore")

    environment: Literal["development", "test", "staging", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    # Externally reachable URL, used in install commands shown to customers.
    public_url: str = "http://127.0.0.1:8080"
    database_url: PostgresDsn = PostgresDsn("postgresql://neura:neura@127.0.0.1:5432/neura")
    database_connect_timeout_s: float = Field(default=2.0, gt=0, le=30)
    database_pool_size: int = Field(default=10, ge=1, le=200)
    openapi_path: Path = _REPO_SPEC
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    # OIDC (customer SSO). When unset, only API tokens authenticate.
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None

    rate_limit_per_minute: int = Field(default=1200, ge=1)
    event_retention_hours: int = Field(default=24, ge=1)
    events_poll_interval_s: float = Field(default=1.0, gt=0, le=30)
    events_keepalive_s: float = Field(default=15.0, gt=0)
    # Streams end after this long; clients reconnect with Last-Event-ID. Bounds connection age
    # behind load balancers and frees server resources from abandoned clients.
    events_max_stream_s: float = Field(default=300.0, gt=0)
    enrollment_token_default_ttl_minutes: int = Field(default=60, ge=5, le=1440)

    # Site agents (design §17): the gRPC listener, the address agents dial (shown in install
    # commands), and the Ed25519 seed (base64url, 32 bytes) desired-state manifests are signed with.
    # Without a seed, development and test generate an ephemeral key; other environments refuse.
    agent_grpc_listen: str | None = "127.0.0.1:8081"
    agent_endpoint: str = "127.0.0.1:8081"
    manifest_signing_key: str | None = None
    agent_token_max_age_s: int = Field(default=300, ge=30, le=3600)
    agent_poll_interval_s: float = Field(default=1.0, gt=0, le=30)
    # TLS for the agent listener: a PEM certificate chain and key, or agent_tls_offloaded when a
    # load balancer in front terminates TLS. Staging and production require one of the two.
    agent_tls_cert_file: Path | None = None
    agent_tls_key_file: Path | None = None
    agent_tls_offloaded: bool = False

    # Development only: on startup, ensure an organization "dev" and an admin API token with this
    # exact value exist. Refused outside development.
    dev_bootstrap_token: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        oidc = [self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url]
        if any(oidc) and not all(oidc):
            raise ValueError("set all of oidc_issuer, oidc_audience and oidc_jwks_url, or none")
        if self.manifest_signing_key is None and self.environment in ("staging", "production"):
            raise ValueError("manifest_signing_key is required in staging and production")
        if (self.agent_tls_cert_file is None) != (self.agent_tls_key_file is None):
            raise ValueError("set both agent_tls_cert_file and agent_tls_key_file, or neither")
        if (
            self.environment in ("staging", "production")
            and self.agent_grpc_listen
            and self.agent_tls_cert_file is None
            and not self.agent_tls_offloaded
        ):
            raise ValueError(
                "the agent listener needs TLS in staging and production: set agent_tls_cert_file"
                " and agent_tls_key_file, or agent_tls_offloaded if a load balancer terminates it"
            )
        if self.dev_bootstrap_token and self.environment != "development":
            raise ValueError("dev_bootstrap_token is only allowed in the development environment")
        return self
