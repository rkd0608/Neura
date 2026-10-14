"""Entry point: ``neura-gateway --config site.yaml``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
from pathlib import Path

from neura_server.config import GatewayConfig
from neura_server.grpc_server import GatewayServer


def main() -> None:
    parser = argparse.ArgumentParser(prog="neura-gateway", description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("NEURA_GATEWAY_CONFIG", "site.yaml")),
        help="gateway configuration file (default: $NEURA_GATEWAY_CONFIG or site.yaml)",
    )
    parser.add_argument("--log-level", default=os.environ.get("NEURA_LOG_LEVEL", "info"))
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    server = GatewayServer(GatewayConfig.load(args.config))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(server.serve_forever())


if __name__ == "__main__":
    main()
