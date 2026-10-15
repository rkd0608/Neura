"""Turning committed state into the data plane's configuration, and supervising the gateway."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import signal
import time
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

from neura_agent.identity import atomic_write
from neura_agent.settings import AgentSettings
from neura_agent.site_pki import GatewayTls, unexpired
from neura_agent.state import AgentState
from neura_server.config import GatewayConfig, SandboxConfig
from neura_server.grpc_server import GatewayServer

log = logging.getLogger("neura.agent.gateway")
HEALTHY_RUN_S = 30.0


def revision_documents(manifest: dict[str, Any], revision_id: str) -> dict[str, Any] | None:
    for deployment in manifest.get("deployments", []):
        for revision in deployment.get("revisions", []):
            if revision["id"] == revision_id:
                documents: dict[str, Any] = revision["documents"]
                return documents
    return None


def build_gateway_config(
    state: AgentState,
    settings: AgentSettings,
    gateway_dir: Path,
    *,
    tls: GatewayTls | None = None,
    site_id: str | None = None,
) -> dict[str, Any]:
    """The gateway's site.yaml: devices with credentials, and for every revision a robot may run
    (current or next), the robots allowed to run it. With ``tls``, robots authenticate with
    certificates from the site CA; without it, with device tokens (development only)."""
    manifest = state.manifest() or {"robots": [], "deployments": []}
    names = {r["name"] for r in manifest.get("robots", [])}
    devices: list[dict[str, Any]] = []
    if tls is not None:
        for name, records in sorted(state.robot_certificates.items()):
            fingerprints = unexpired(records)
            if name in names and fingerprints:
                devices.append({"robot_id": name, "certificate_sha256": fingerprints})
    else:
        credentials = dict(state.robot_credentials)
        for name, token in (settings.dev_robot_tokens or {}).items():
            credentials.setdefault(name, hashlib.sha256(token.encode()).hexdigest())
        devices = [
            {"robot_id": name, "token_sha256": digest}
            for name, digest in sorted(credentials.items())
            if name in names
        ]
    by_revision: dict[str, list[str]] = {}
    for name, assignment in sorted(state.assignments.items()):
        if name not in names:
            continue
        for revision_id in assignment.allowed():
            by_revision.setdefault(revision_id, []).append(name)
    deployments = []
    workers = settings.gateway_backend == "workers"
    for revision_id, robots in sorted(by_revision.items()):
        documents = revision_documents(manifest, revision_id)
        if documents is None or revision_id not in state.prepared:
            continue
        placement = state.placements.get(revision_id)
        if workers and placement is None:
            continue  # prepared before the workers backend was configured: prepare again
        paths = write_revision_documents(gateway_dir, revision_id, documents)
        deployment: dict[str, Any] = {
            "revision_id": revision_id,
            "artifact": str(paths["model_artifact"]),
            "robot_profile": str(paths["robot_profile"]),
            "execution_profile": str(paths["execution_profile"]),
            "robots": robots,
        }
        if workers and placement is not None:
            deployment["device"] = placement["device"]
            for key in ("checkpoint", "profile", "adapter"):
                if placement.get(key) is not None:
                    deployment[key] = placement[key]
        deployments.append(deployment)
    served = {d["revision_id"] for d in deployments}
    assignments = {
        name: preferred
        for name, assignment in sorted(state.assignments.items())
        if name in names and (preferred := assignment.preferred()) in served
    }
    config: dict[str, Any] = {
        "site_id": site_id,
        "listen": settings.gateway_listen,
        "status_listen": settings.gateway_status_listen,
        "lease_store": str(gateway_dir / "leases.db"),
        "devices": devices,
        "deployments": deployments,
        "assignments": assignments,
        "serving": {
            "backend": settings.gateway_backend,
            "simulate_latency": settings.gateway_simulate_latency,
            "max_batch": settings.gateway_max_batch,
            "worker_command": settings.worker_command,
            "sandbox": sandbox_config(settings).model_dump(mode="json"),
        },
    }
    if tls is not None:
        config["tls"] = {
            "certificate": str(tls.certificate),
            "key": str(tls.key),
            "client_ca": str(tls.client_ca),
        }
    return config


def sandbox_config(settings: AgentSettings) -> SandboxConfig:
    return SandboxConfig(
        kind=settings.worker_sandbox,
        runtime=settings.worker_container_runtime,
        image_override=settings.worker_image_override,
        memory_mib=settings.worker_memory_mib,
        cpus=settings.worker_cpus,
        pids_limit=settings.worker_pids_limit,
    )


def write_revision_documents(
    gateway_dir: Path, revision_id: str, documents: dict[str, Any]
) -> dict[str, Path]:
    """The revision's contract documents as files the gateway and its workers read."""
    directory = gateway_dir / "revisions" / revision_id
    paths = {}
    for role, filename in (
        ("model_artifact", "artifact.json"),
        ("robot_profile", "robot.json"),
        ("execution_profile", "profile.json"),
    ):
        path = directory / filename
        data = json.dumps(documents[role]["document"]).encode()
        if not path.exists() or path.read_bytes() != data:
            atomic_write(path, data, 0o644)
        paths[role] = path
    return paths


def write_gateway_config(config: dict[str, Any], path: Path) -> bool:
    """Write the config if it changed. Returns True when it did."""
    GatewayConfig.model_validate(config)  # never hand the gateway something it would reject
    data = yaml.safe_dump(config, sort_keys=True).encode()
    if path.exists() and path.read_bytes() == data:
        return False
    atomic_write(path, data, 0o644)
    return True


class GatewayRunner(Protocol):
    async def start(self, config_path: Path) -> None: ...
    async def reload(self) -> None: ...
    async def stop(self) -> None: ...
    async def status(self) -> dict[str, Any] | None: ...

    @property
    def running(self) -> bool: ...


class SubprocessGateway:
    """Runs ``neura-gateway`` as a supervised child process; restarts it if it exits."""

    def __init__(self, command: list[str], status_listen: str) -> None:
        self._command = command
        host, _, port = status_listen.rpartition(":")
        host = "127.0.0.1" if host in ("0.0.0.0", "") else host  # noqa: S104 - client side
        self._status_url = f"http://{host}:{port}/v1/local/dataplane"
        self._process: asyncio.subprocess.Process | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._config: Path | None = None
        self._stopping = False
        self.restarts = 0

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, config_path: Path) -> None:
        if self._supervisor is not None:
            return
        self._config = config_path
        self._stopping = False
        self._supervisor = asyncio.create_task(self._supervise(), name="gateway-supervisor")

    def _stop_requested(self) -> bool:
        return self._stopping

    async def _supervise(self) -> None:
        backoff = 0.5
        while not self._stop_requested():
            assert self._config is not None
            self._process = await asyncio.create_subprocess_exec(
                *self._command, "--config", str(self._config)
            )
            log.info("gateway started (pid %s)", self._process.pid)
            started = time.monotonic()
            code = await self._process.wait()
            if self._stop_requested():  # stop() may have run while we waited
                return
            if time.monotonic() - started > HEALTHY_RUN_S:
                backoff = 0.5  # it ran fine for a while: this is a new failure, not a crash loop
            log.warning("gateway exited with %s; restarting in %.1fs", code, backoff)
            self.restarts += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    async def reload(self) -> None:
        if self.running:
            assert self._process is not None
            self._process.send_signal(signal.SIGHUP)

    async def stop(self) -> None:
        self._stopping = True
        if self.running:
            assert self._process is not None
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except TimeoutError:
                self._process.kill()
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
        self._supervisor = None
        self._process = None

    async def status(self) -> dict[str, Any] | None:
        if not self.running:
            return None
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                response = await client.get(self._status_url)
            data: dict[str, Any] = response.json()
            return data
        except (httpx.HTTPError, ValueError):
            return None


class InProcessGateway:
    """Runs the gateway inside the agent's event loop. Used by tests and single-process setups."""

    def __init__(self) -> None:
        self.server: GatewayServer | None = None
        self._config: Path | None = None

    @property
    def running(self) -> bool:
        return self.server is not None

    async def start(self, config_path: Path) -> None:
        if self.server is not None:
            return
        self._config = config_path
        self.server = GatewayServer(GatewayConfig.load(config_path))
        await self.server.start()

    async def reload(self) -> None:
        if self.server is not None and self._config is not None:
            self.server.reload(GatewayConfig.load(self._config))

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()
            self.server = None

    async def status(self) -> dict[str, Any] | None:
        return self.server.status() if self.server is not None else None
