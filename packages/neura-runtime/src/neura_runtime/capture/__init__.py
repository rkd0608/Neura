"""Episode capture on the robot (design §28.2): record, spool, upload to the site.

``RobotCapture`` wires the three for one robot process. The integration ticks it after each
control period and may label an episode's outcome; everything else follows the runtime.
"""

from __future__ import annotations

from pathlib import Path

from neura_runtime.capture.episode import (
    FORMAT,
    EndReason,
    Episode,
    EpisodeError,
    Outcome,
    OutcomeSource,
    open_episode,
)
from neura_runtime.capture.recorder import CaptureConfig, EpisodeRecorder, StateSource
from neura_runtime.capture.spool import Spool
from neura_runtime.capture.uploader import Uploader, capture_target
from neura_runtime.grpc_client import RobotCredentials
from neura_runtime.interfaces import SensorSource
from neura_runtime.runtime import RobotRuntime

__all__ = [
    "FORMAT",
    "CaptureConfig",
    "EndReason",
    "Episode",
    "EpisodeError",
    "EpisodeRecorder",
    "Outcome",
    "OutcomeSource",
    "RobotCapture",
    "Spool",
    "Uploader",
    "open_episode",
]


class RobotCapture:
    """Recording for one robot process: the recorder on the control loop, the spool's writer
    thread, and the uploader thread."""

    def __init__(
        self,
        runtime: RobotRuntime,
        config: CaptureConfig,
        *,
        directory: Path,
        robot_id: str,
        revision_id: str,
        sensors: SensorSource,
        gateway_target: str,
        credentials: RobotCredentials,
        state_source: StateSource | None = None,
        label_grace_s: float = 5.0,
    ) -> None:
        self.config = config
        self.spool = Spool(directory, max_bytes=config.spool_max_bytes)
        self.recorder = EpisodeRecorder(
            runtime,
            config,
            self.spool,
            robot_id=robot_id,
            revision_id=revision_id,
            sensors=sensors,
            state_source=state_source,
        )
        target = capture_target(config.endpoint, gateway_target)
        self.uploader = Uploader(
            self.spool,
            target,
            lambda t: credentials.sync_channel(t),
            metadata=credentials.metadata(),
            bytes_per_second=config.upload_bytes_per_second,
            label_grace_s=label_grace_s,
        )
        self.recorder.attach()
        self.uploader.start()

    def tick(self, now_us: int) -> None:
        self.recorder.tick(now_us)

    def label(
        self, outcome: Outcome, source: OutcomeSource = OutcomeSource.ROBOT, note: str = ""
    ) -> str | None:
        return self.recorder.label(outcome, source, note)

    def close(self, now_us: int) -> None:
        """End recording; queued episodes stay in the spool and upload next time."""
        self.recorder.detach(now_us)
        self.spool.stop()
        self.uploader.nudge()
        self.uploader.stop(timeout_s=2.0)
