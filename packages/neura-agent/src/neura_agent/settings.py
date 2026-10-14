"""Site-agent configuration, read from ``NEURA_AGENT_*`` environment variables."""

from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEURA_AGENT_", frozen=True, extra="ignore")

    control_plane_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8080")
    # The local site API binds to loopback by default; operators expose it deliberately.
    local_host: str = "127.0.0.1"
    local_port: int = Field(default=8090, ge=1, le=65535)
    probe_interval_s: float = Field(default=5.0, gt=0, le=300)
    probe_timeout_s: float = Field(default=2.0, gt=0, le=60)
    log_level: Literal["debug", "info", "warning", "error"] = "info"
