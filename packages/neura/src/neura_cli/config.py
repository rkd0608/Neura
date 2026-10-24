"""Where the CLI keeps what it needs between commands: the API's address, a token, the workspace,
the publisher key it signs with, and the sites this machine runs.

``~/.config/neura/config.json`` (``$NEURA_CONFIG_DIR``), readable by its owner only. The
environment overrides it: ``NEURA_API_URL``, ``NEURA_TOKEN``, ``NEURA_WORKSPACE`` (for CI).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_API = "http://127.0.0.1:8080"


def config_dir() -> Path:
    configured = os.environ.get("NEURA_CONFIG_DIR")
    if configured:
        return Path(configured)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "neura"


@dataclass
class Config:
    api_url: str = DEFAULT_API
    token: str | None = None
    organization_id: str | None = None
    project_id: str | None = None
    # Sites this machine runs with `neura site join`: their agent's state directory, so a policy
    # pushed here reaches them without any upload.
    local_sites: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> Config:
        path = config_dir() / "config.json"
        data: dict[str, Any] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        known = set(cls.__dataclass_fields__) - {"_saved_token"}
        config = cls(**{k: v for k, v in data.items() if k in known})
        config.api_url = os.environ.get("NEURA_API_URL", config.api_url)
        config._saved_token = config.token
        if os.environ.get("NEURA_TOKEN"):
            config.token = os.environ["NEURA_TOKEN"]
            config.project_id = None  # a token from the environment names its own workspace
            config.organization_id = os.environ.get("NEURA_WORKSPACE")
        return config

    _saved_token: str | None = field(default=None, repr=False, compare=False)

    def save(self) -> None:
        """Write the configuration; a token from the environment is never written."""
        directory = config_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "config.json"
        data = asdict(self)
        data.pop("_saved_token")
        if os.environ.get("NEURA_TOKEN"):
            data["token"] = self._saved_token
            data["project_id"] = data["organization_id"] = None
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def publisher_key_path(self) -> Path:
        return config_dir() / "publisher.key"
