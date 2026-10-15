"""Control-plane configuration, read from ``NEURA_CONTROL_*`` environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self
from urllib.parse import parse_qs, urlsplit

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
    # Staging and production connect to Postgres over TLS that verifies the server
    # (sslmode=verify-full in database_url), unless the connection never leaves the host (a
    # Unix socket or a local proxy that does the TLS): then set this.
    database_tls_local: bool = False
    database_connect_timeout_s: float = Field(default=2.0, gt=0, le=30)
    database_pool_size: int = Field(default=10, ge=1, le=200)
    openapi_path: Path = _REPO_SPEC
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    # OIDC (customer SSO). When unset, only API tokens authenticate.
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    # Accept only tokens issued to these clients (azp or client_id): the console's and any CLI's.
    # Empty: any client of the issuer whose token names this audience.
    oidc_allowed_clients: list[str] = Field(default_factory=list)
    # Require these authentication methods (amr), e.g. ["mfa"], when the identity provider
    # reports them. Empty: multi-factor sign-in is enforced by the identity provider's policy.
    oidc_required_amr: list[str] = Field(default_factory=list)
    # The console signs people in with authorization code + PKCE as a public client registered at
    # the IdP. Published in /v1/meta; without it the console offers API-token sign-in only.
    console_oidc_client_id: str | None = None
    console_oidc_scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])

    rate_limit_per_minute: int = Field(default=1200, ge=1)
    # Every request also counts against its client address, so a flood of distinct (invalid)
    # credentials from one place is limited too. Behind a proxy, list it in forwarded_allow_ips so
    # the address is the client's, not the proxy's.
    rate_limit_per_address_per_minute: int = Field(default=6000, ge=1)
    forwarded_allow_ips: str = "127.0.0.1"
    # Beyond this many requests in progress the server answers 503 at once.
    max_concurrent_requests: int = Field(default=1000, ge=1)
    # Open change-event streams per process; beyond it a new stream is told to retry.
    events_max_streams: int = Field(default=200, ge=1)
    # Concurrent RPCs per site-agent gRPC listener.
    agent_max_concurrent_rpcs: int = Field(default=2000, ge=1)
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
    # With TLS, agents' streams require their client certificates, which a server gets at
    # enrollment, so enrollment has a listener of its own (server-authenticated TLS, Enroll
    # only). Without TLS (development) one listener serves both.
    agent_enroll_listen: str | None = None
    agent_enroll_endpoint: str | None = None  # default: agent_endpoint
    manifest_signing_key: str | None = None
    agent_token_max_age_s: int = Field(default=300, ge=30, le=3600)
    agent_poll_interval_s: float = Field(default=1.0, gt=0, le=30)
    # TLS for the agent listener: a PEM certificate chain and key, or agent_tls_offloaded when a
    # load balancer in front terminates TLS. Staging and production require one of the two.
    agent_tls_cert_file: Path | None = None
    agent_tls_key_file: Path | None = None
    agent_tls_offloaded: bool = False
    # The CA that issues site agents their TLS client certificates (mutual TLS on the stream).
    # Required in staging and production; development uses a CA generated at startup.
    agent_client_ca_cert_file: Path | None = None
    agent_client_ca_key_file: Path | None = None
    agent_certificate_days: int = Field(default=30, ge=1, le=397)

    # Prometheus metrics on their own listener (host:port), apart from the public API. Unset:
    # no metrics listener.
    metrics_listen: str | None = None

    # Neura's registry of sealed weights (design §18.2): a directory (local disk, or a shared
    # filesystem when several control-plane replicas run). Unset: the registry is off and
    # customers keep weights in their own stores.
    sealed_weights_dir: Path | None = None
    sealed_weights_max_bytes: int = Field(default=64 << 30, ge=1)

    # Development only: on startup, ensure an organization "dev" and an admin API token with this
    # exact value exist. Refused outside development.
    dev_bootstrap_token: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        oidc = [self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url]
        if any(oidc) and not all(oidc):
            raise ValueError("set all of oidc_issuer, oidc_audience and oidc_jwks_url, or none")
        if self.console_oidc_client_id and not self.oidc_issuer:
            raise ValueError("console_oidc_client_id needs the OIDC settings (oidc_issuer, ...)")
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
        if (
            self.agent_tls_cert_file is not None
            and self.agent_grpc_listen
            and not (self.agent_enroll_listen)
        ):
            raise ValueError(
                "with TLS the agent stream requires client certificates: set agent_enroll_listen"
                " (and agent_enroll_endpoint) for enrollment"
            )
        if (self.agent_client_ca_cert_file is None) != (self.agent_client_ca_key_file is None):
            raise ValueError(
                "set both agent_client_ca_cert_file and agent_client_ca_key_file, or neither"
            )
        if (
            self.environment in ("staging", "production")
            and self.agent_grpc_listen
            and self.agent_client_ca_cert_file is None
        ):
            raise ValueError(
                "site agents need client certificates in staging and production: set"
                " agent_client_ca_cert_file and agent_client_ca_key_file"
            )
        sslmode = parse_qs(urlsplit(str(self.database_url)).query).get("sslmode", [""])[-1]
        if (
            self.environment in ("staging", "production")
            and not self.database_tls_local
            and sslmode != "verify-full"
        ):
            raise ValueError(
                "staging and production connect to Postgres with sslmode=verify-full (or set"
                " database_tls_local for a Unix socket or a local TLS proxy)"
            )
        if self.dev_bootstrap_token and self.environment != "development":
            raise ValueError("dev_bootstrap_token is only allowed in the development environment")
        return self
