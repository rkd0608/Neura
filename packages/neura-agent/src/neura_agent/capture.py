"""The site's episode store and the capture service robots upload to (design §28.2).

Hosted by the agent, not the gateway: uploads must never compete with the action path. The
service follows the gateway's configuration: the same robots and credentials, and only the
revisions whose deployments capture (within the site's boundary) may upload.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from neura.v1 import site_pb2
from neura_agent.settings import AgentSettings
from neura_server.capture.service import CaptureServer
from neura_server.capture.store import EpisodeStore
from neura_server.config import DeviceConfig, DeviceRegistry, TlsConfig

log = logging.getLogger("neura.agent.capture")


def capture_pairs(gateway_config: dict[str, Any]) -> set[tuple[str, str]]:
    """(robot, revision) pairs whose deployment captures episodes."""
    return {
        (robot, deployment["revision_id"])
        for deployment in gateway_config.get("deployments", [])
        if deployment.get("capture")
        for robot in deployment["robots"]
    }


class SiteCapture:
    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings
        self.enabled = settings.capture_allowed != "off"
        root = settings.capture_dir or settings.state_dir / "capture"
        self.store = EpisodeStore(
            Path(root),
            max_bytes=int(settings.capture_max_gib * 1024**3),
            max_age_days=settings.capture_max_age_days,
        )
        self.server: CaptureServer | None = None
        self._pairs: set[tuple[str, str]] = set()

    def allowed(self, robot_id: str, revision_id: str) -> bool:
        return (robot_id, revision_id) in self._pairs

    async def configure(self, gateway_config: dict[str, Any]) -> None:
        """Follow the gateway's robots, credentials and capturing revisions; start the service
        the first time a deployment captures."""
        if not self.enabled:
            return
        self._pairs = capture_pairs(gateway_config)
        registry = DeviceRegistry(
            [DeviceConfig.model_validate(d) for d in gateway_config.get("devices", [])],
            site_id=gateway_config.get("site_id"),
        )
        if self.server is not None:
            self.server.update(registry, self.allowed)
            return
        if not self._pairs:
            return  # nothing records yet: no port opened
        tls = gateway_config.get("tls")
        self.server = CaptureServer(
            self.store,
            self.settings.capture_listen,
            registry=registry,
            tls=TlsConfig.model_validate(tls) if tls else None,
            allowed=self.allowed,
            on_stored=lambda record: log.info(
                "stored episode %s from %s (%s)",
                record["episode_id"],
                record["robot_id"],
                record["outcome"],
            ),
        )
        await self.server.start()

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()
            self.server = None

    async def retention(self) -> None:
        deleted = await asyncio.to_thread(self.store.enforce_retention)
        if deleted:
            log.info("retention deleted %d episodes", len(deleted))

    def summary_message(self) -> site_pb2.CaptureSummary:
        """Counts for the site's status report: never an episode's content."""
        summary = self.store.summary()
        return site_pb2.CaptureSummary(
            enabled=self.enabled,
            episodes=summary["episodes"],
            bytes=summary["bytes"],
            steps=summary["steps"],
            outcomes=summary["outcomes"],
            revisions=[
                site_pb2.RevisionCapture(revision_id=revision_id, outcomes=outcomes)
                for revision_id, outcomes in sorted(summary["revisions"].items())
            ],
            datasets=summary["datasets"],
        )
