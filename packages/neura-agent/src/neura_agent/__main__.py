"""Entry point: ``neura-agent`` / ``python -m neura_agent``."""

from __future__ import annotations

import uvicorn

from neura_agent.app import create_app
from neura_agent.settings import AgentSettings


def main() -> None:
    settings = AgentSettings()
    uvicorn.run(
        create_app(settings),
        host=settings.local_host,
        port=settings.local_port,
        log_level=settings.log_level,
        server_header=False,
    )


if __name__ == "__main__":
    main()
