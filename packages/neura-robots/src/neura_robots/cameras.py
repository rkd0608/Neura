"""Cameras for robot integrations.

Real cameras are LeRobot's (OpenCV, RealSense), which capture on a background thread and keep the
latest frame with its capture time. ``latest_frame`` reads both together and converts the capture
time to the robot's monotonic clock by its age, so the observation carries when the image was
taken, not when it was read (design §5.2, §8).

``SyntheticCamera`` behaves the same way for the emulated arm: a moving test pattern at a fixed
rate, with faults (frozen, unplugged, lagging) for tests and qualification.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray


class CameraLike(Protocol):
    width: Any
    height: Any

    @property
    def is_connected(self) -> bool: ...

    def connect(self, warmup: bool = True) -> None: ...

    def disconnect(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Frame:
    image: NDArray[np.uint8]  # height x width x 3, RGB
    age_us: int  # how long ago it was captured


def latest_frame(camera: Any) -> Frame | None:
    """The newest frame of a LeRobot-style camera and its age, or None if there is none."""
    lock = getattr(camera, "frame_lock", None)
    if lock is None:
        return None
    with lock:
        image = getattr(camera, "latest_frame", None)
        captured = getattr(camera, "latest_timestamp", None)
    if image is None or captured is None:
        return None
    age = max(0, int((time.perf_counter() - captured) * 1_000_000))
    return Frame(image, age)


class SyntheticCamera:
    """A camera for the emulated arm: frames at ``fps`` with the LeRobot camera attributes."""

    def __init__(self, width: int, height: int, fps: float = 30.0, *, seed: int = 0) -> None:
        self.width, self.height, self.fps = width, height, fps
        self.frame_lock = threading.Lock()
        self.latest_frame: NDArray[np.uint8] | None = None
        self.latest_timestamp: float | None = None
        self.frozen = False  # stops producing frames (a hung driver)
        self.lag_s = 0.0  # frames carry an older capture time (a buffering pipeline)
        self._seed = seed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._count = 0

    @property
    def is_connected(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def connect(self, warmup: bool = True) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="synthetic-camera", daemon=True)
        self._thread.start()
        if warmup:
            deadline = time.monotonic() + 2.0
            while self.latest_frame is None and time.monotonic() < deadline:
                time.sleep(0.005)

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        with self.frame_lock:
            self.latest_frame = None
            self.latest_timestamp = None

    def _loop(self) -> None:
        period = 1.0 / self.fps
        base = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        while not self._stop.wait(period):
            if self.frozen:
                continue
            self._count += 1
            image = base.copy()
            row = (self._count * 7 + self._seed) % self.height
            image[row, :, :] = 255  # a moving line: consecutive frames differ
            with self.frame_lock:
                self.latest_frame = image
                self.latest_timestamp = time.perf_counter() - self.lag_s
