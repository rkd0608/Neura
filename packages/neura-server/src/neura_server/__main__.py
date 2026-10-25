"""Entry point: ``neura-gateway --config site.yaml``. SIGHUP reloads devices and deployments;
SIGTERM and SIGINT stop the gateway, its workers with it."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
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

    async def run() -> None:
        loop = asyncio.get_running_loop()

        def reload() -> None:
            try:
                server.reload(GatewayConfig.load(args.config))
            except Exception:
                logging.getLogger("neura.gateway").exception("reload failed; keeping old config")

        stopping = asyncio.Event()
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGHUP, reload)
            for stop in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(stop, stopping.set)
        await server.serve_forever(until=stopping)  # stops everything it started

    asyncio.run(run())


if __name__ == "__main__":
    main()
