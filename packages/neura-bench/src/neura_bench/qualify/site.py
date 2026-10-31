"""The site a robot connects to during qualification and the evidence runs: the site's gateway
process, with its real policy workers, behind the TCP fault proxy, on this machine (design §7.1).
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from neura_server.config import token_digest
from neura_sim.chaos import FaultProxy, GatewayProcess, free_port

ROBOT_ID = "qualify-arm"
TOKEN = "qualify-token"  # noqa: S105 - the qualification gateway listens on 127.0.0.1 only
REVISION = "rev_qualify"


class LocalSite:
    """The site's gateway process, with its real policy workers, behind the fault proxy, on
    this machine: what the robot connects to during qualification."""

    def __init__(
        self,
        artifact: Path,
        robot_profile: Path,
        execution_profile: Path,
        checkpoint: Path | None,
        device: str,
        workdir: Path,
        start_timeout_s: float = 180.0,
    ) -> None:
        workdir = workdir.resolve()
        self.workdir = workdir
        gateway_port, status_port, proxy_port = free_port(), free_port(), free_port()
        # Absolute: the gateway and its workers run in the site's directory, not the caller's.
        deployment: dict[str, Any] = {
            "revision_id": REVISION,
            "artifact": str(artifact.resolve()),
            "robot_profile": str(robot_profile.resolve()),
            "execution_profile": str(execution_profile.resolve()),
            "robots": [ROBOT_ID],
            "device": device,
        }
        if checkpoint is not None:
            deployment["checkpoint"] = str(checkpoint.resolve())
        else:
            deployment["adapter"] = "mock"
        config = {
            "listen": f"127.0.0.1:{gateway_port}",
            "status_listen": f"127.0.0.1:{status_port}",
            "lease_store": str(workdir / "leases.sqlite"),
            "lease_ttl_ms": 2000,
            "devices": [{"robot_id": ROBOT_ID, "token_sha256": token_digest(TOKEN)}],
            "deployments": [deployment],
            "serving": {
                "backend": "workers",
                "worker_timeout_ms": 2000,
                "worker_start_timeout_s": int(start_timeout_s),
                "worker_dir": str(workdir / "w"),
            },
        }
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / "site.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        self.gateway = GatewayProcess(path, workdir / "gateway.log")
        self.proxy = FaultProxy(proxy_port, gateway_port)
        self.target = f"127.0.0.1:{proxy_port}"
        self.status_url = f"http://127.0.0.1:{status_port}/v1/local/dataplane"

    def status(self) -> dict[str, Any]:
        """The gateway's local status API (empty while it starts)."""
        try:
            with urllib.request.urlopen(self.status_url, timeout=2) as response:  # noqa: S310 - loopback
                status: dict[str, Any] = json.load(response)
        except (OSError, ValueError):
            return {}
        return status

    def workers(self) -> list[dict[str, Any]]:
        groups = self.status().get("groups", {}).values()
        return [g["worker"] for g in groups if "worker" in g]

    async def workers_ready(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            workers = await asyncio.to_thread(self.workers)
            if workers and all(w.get("state") == "ready" for w in workers):
                return True
            await asyncio.sleep(0.25)
        return False

    async def __aenter__(self) -> LocalSite:
        # The proxy first: its port is what can be taken, and when it is nothing has started
        # yet. A gateway started first would outlive the error (__aexit__ does not run).
        await self.proxy.start()
        try:
            await self.gateway.start()
        except BaseException:
            await self.proxy.stop()
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.proxy.stop()
        await self.gateway.stop()
