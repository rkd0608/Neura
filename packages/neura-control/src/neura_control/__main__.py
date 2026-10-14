"""Entry point: ``neura-control`` / ``python -m neura_control``."""

from __future__ import annotations

import uvicorn

from neura_control.app import create_app
from neura_control.settings import ControlPlaneSettings


def main() -> None:
    settings = ControlPlaneSettings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        proxy_headers=True,
        server_header=False,
    )


if __name__ == "__main__":
    main()
