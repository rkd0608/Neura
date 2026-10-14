"""Control-plane configuration, read from ``NEURA_CONTROL_*`` environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_SPEC = Path(__file__).resolve().parents[4] / "api" / "openapi.yaml"


class ControlPlaneSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEURA_CONTROL_", frozen=True, extra="ignore")

    environment: Literal["development", "test", "staging", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    database_url: PostgresDsn = PostgresDsn("postgresql://neura:neura@127.0.0.1:5432/neura")
    database_connect_timeout_s: float = Field(default=2.0, gt=0, le=30)
    openapi_path: Path = _REPO_SPEC
    log_level: Literal["debug", "info", "warning", "error"] = "info"
