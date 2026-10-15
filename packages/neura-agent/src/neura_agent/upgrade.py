"""Signed agent upgrades with automatic rollback (design §17.3).

1. **Fetch and verify.** The bundle is fetched (https:// or file://) and checked against the
   SHA-256 in the command, then against the release keys pinned on this server: a bundle the
   control plane names but Neura did not sign is refused.
2. **Stage.** The release gets its own virtual environment from the bundle's wheels.
3. **Wait for a task boundary.** The switch restarts the agent and its gateway, so it waits until
   no robot is in a task (armed, running or waiting), for at most ``upgrade_wait_s``.
4. **Switch.** ``current`` points at the new release, the upgrade is recorded as switching, and
   the agent restarts (systemd starts it again through the launcher).
5. **Health check.** The new agent commits the upgrade once it has connected to the control plane
   again. If it does not within ``upgrade_health_timeout_s``, it switches back and restarts; if it
   cannot even start, the launcher rolls back after three attempts. The old agent then reports
   the rollback.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from neura.v1 import site_pb2
from neura_agent.releases import (
    Installation,
    ReleaseError,
    UpgradeState,
    load_release_keys,
    unpack,
    verify_bundle,
)
from neura_contracts import ReasonCode
from neura_runtime.runtime import ACTIVE_STATES

if TYPE_CHECKING:
    from neura_agent.core import AgentCore

log = logging.getLogger("neura.agent.upgrade")

CHUNK = 1 << 20


class Upgrader:
    def __init__(
        self,
        core: AgentCore,
        *,
        version: str,
        connected: Callable[[], bool],
        restart: Callable[[], None],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.core = core
        self.settings = core.settings
        self.version = version
        self.installation = Installation(core.settings.install_dir)
        self._connected = connected
        self._restart = restart
        self._monotonic = monotonic
        self._started = monotonic()
        self._task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def in_progress(self) -> set[str]:
        """Command ids of an upgrade the next agent finishes reporting (not interrupted)."""
        state = UpgradeState.load(self.settings.state_dir)
        return {state.command_id} if state is not None else set()

    # --- the upgrade command ------------------------------------------------------------------

    def start(self, command_id: str, operation_id: str, request: site_pb2.UpgradeAgent) -> None:
        if self.busy:
            self.core.report(
                command_id,
                operation_id,
                "Failed",
                reason=ReasonCode.AGENT_UPGRADE_REFUSED,
                message="another agent upgrade is in progress",
            )
            return
        self._task = asyncio.create_task(self.run(command_id, operation_id, request))

    async def run(self, command_id: str, operation_id: str, request: site_pb2.UpgradeAgent) -> None:
        def fail(code: ReasonCode, message: str) -> None:
            self.core.audit.write("agent.upgrade.failed", version=request.version, reason=code)
            self.core.report(command_id, operation_id, "Failed", reason=code, message=message)

        if not self.installation.installed:
            fail(
                ReasonCode.AGENT_UPGRADE_REFUSED,
                "this agent was not installed with the installer; upgrade it the way it was "
                "installed",
            )
            return
        if request.version == self.version:
            self.core.report(
                command_id,
                operation_id,
                "Succeeded",
                result={"agent_version_from": self.version, "agent_version_to": self.version},
            )
            return
        self.core.report(command_id, operation_id, "Running", message="fetching the release")
        with tempfile.TemporaryDirectory(dir=self._scratch()) as scratch:
            try:
                keys = load_release_keys(self.settings.release_keys_file)
                bundle = await self._fetch(request, Path(scratch) / "bundle.tar")
                directory = await asyncio.to_thread(unpack, bundle, Path(scratch) / "bundle")
                release = verify_bundle(directory, keys)
                if release.version != request.version:
                    raise ReleaseError(
                        f"the bundle is release {release.version}, not {request.version}"
                    )
                self.core.report(
                    command_id, operation_id, "Running", message=f"staging {release.version}"
                )
                await asyncio.to_thread(self.installation.stage, directory, release)
            except ReleaseError as exc:
                fail(ReasonCode.AGENT_UPGRADE_REFUSED, str(exc))
                return
        self.core.audit.write("agent.upgrade.staged", version=request.version)
        self.core.report(
            command_id,
            operation_id,
            "Running",
            message=f"{request.version} staged; switching when every robot is between tasks",
        )
        if not await self._task_boundary():
            fail(
                ReasonCode.AGENT_UPGRADE_DEFERRED,
                f"robots were in tasks for {self.settings.upgrade_wait_s:.0f} s",
            )
            return
        UpgradeState(
            command_id=command_id,
            operation_id=operation_id,
            from_version=self.version,
            to_version=request.version,
            state="switching",
        ).save(self.settings.state_dir)
        self.installation.switch(request.version)
        self.core.audit.write(
            "agent.upgrade.switch", version=request.version, previous=self.version
        )
        self.core.report(
            command_id,
            operation_id,
            "Running",
            message=f"switched to {request.version}; the agent restarts",
        )
        self._restart()

    def _scratch(self) -> Path:
        path = self.settings.state_dir / "upgrades"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _fetch(self, request: site_pb2.UpgradeAgent, destination: Path) -> Path:
        url = urllib.parse.urlparse(request.bundle_url)
        digest = hashlib.sha256()
        if url.scheme == "file":
            source = Path(urllib.parse.unquote(url.path))
            try:
                with source.open("rb") as src, destination.open("wb") as dst:
                    while block := src.read(CHUNK):
                        digest.update(block)
                        dst.write(block)
            except OSError as exc:
                raise ReleaseError(f"cannot read {source}: {exc.strerror}") from exc
        elif url.scheme == "https":
            try:
                async with (
                    httpx.AsyncClient(timeout=60, follow_redirects=True) as client,
                    client.stream("GET", request.bundle_url) as response,
                ):
                    response.raise_for_status()
                    with destination.open("wb") as dst:
                        async for block in response.aiter_bytes(CHUNK):
                            digest.update(block)
                            dst.write(block)
            except httpx.HTTPError as exc:
                raise ReleaseError(f"cannot fetch the release: {exc}") from exc
        else:
            raise ReleaseError("release bundles are fetched over https:// or from file://")
        if digest.hexdigest() != request.bundle_sha256.lower():
            raise ReleaseError("the bundle does not match the SHA-256 in the command")
        return destination

    async def _task_boundary(self) -> bool:
        deadline = self._monotonic() + self.settings.upgrade_wait_s
        while True:
            status = await self.core.gateway.status()
            sessions = (status or {}).get("sessions", [])
            if not any(s.get("robot_state") in ACTIVE_STATES for s in sessions):
                return True
            if self._monotonic() >= deadline:
                return False
            await asyncio.sleep(1.0)

    # --- after a restart ----------------------------------------------------------------------

    def check(self) -> None:
        """Commit or roll back an upgrade this agent is the result of; report a rollback this
        agent is the survivor of. Called at startup and then periodically."""
        state_dir = self.settings.state_dir
        state = UpgradeState.load(state_dir)
        if state is None:
            return
        if state.state == "switching" and self.version == state.to_version:
            if self._connected():
                state.state = "committed"
                state.save(state_dir)
                self.installation.prune({state.to_version, state.from_version})
                self.core.audit.write(
                    "agent.upgrade.committed", version=state.to_version, previous=state.from_version
                )
                self.core.report(
                    state.command_id,
                    state.operation_id,
                    "Succeeded",
                    result=self._result(state),
                )
                UpgradeState.clear(state_dir)
            elif self._monotonic() - self._started > self.settings.upgrade_health_timeout_s:
                state.state = "rolled_back"
                state.reason = (
                    f"neura-agent {state.to_version} did not reconnect to Neura within "
                    f"{self.settings.upgrade_health_timeout_s:.0f} s"
                )
                state.save(state_dir)
                self.installation.switch(state.from_version)
                self.core.audit.write("agent.upgrade.rollback", reason=state.reason)
                log.error("rolling back: %s", state.reason)
                self._restart()
        elif state.state == "rolled_back" and self.version == state.from_version:
            self.core.audit.write("agent.upgrade.rolled_back", reason=state.reason)
            self.core.report(
                state.command_id,
                state.operation_id,
                "Failed",
                reason=ReasonCode.AGENT_UPGRADE_ROLLED_BACK,
                message=state.reason,
                result=self._result(state),
            )
            UpgradeState.clear(state_dir)

    @staticmethod
    def _result(state: UpgradeState) -> dict[str, Any]:
        return {"agent_version_from": state.from_version, "agent_version_to": state.to_version}
