"""Uploads finished episodes from the robot's spool to the site (design §28.2).

A thread of its own, with a connection of its own: an upload never shares the control loop or
the gateway stream. It sends one episode at a time, oldest first, at no more than the configured
bandwidth, and deletes an episode only once the site has acknowledged it. A failed upload goes
back to the spool and is retried with backoff.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

import grpc

from neura.v1 import capture_pb2, capture_pb2_grpc
from neura_runtime.capture.episode import MANIFEST, canonical, check_manifest, is_failure
from neura_runtime.capture.spool import Spool

log = logging.getLogger("neura.capture")

PART_BYTES = 1024 * 1024
# A closed episode waits this long for an outcome label before it is uploaded.
LABEL_GRACE_S = 5.0


def capture_target(endpoint: str, gateway_target: str) -> str:
    """``host:port``, or ``:port`` on the gateway's host."""
    if endpoint.startswith(":"):
        host = gateway_target.rsplit(":", 1)[0]
        return f"{host}{endpoint}"
    return endpoint


class _Throttle:
    def __init__(self, bytes_per_second: int, clock: Callable[[], float] = time.monotonic):
        self.rate = bytes_per_second
        self._clock = clock
        self._allowance = float(bytes_per_second)
        self._last = clock()

    def wait(self, size: int, stop: threading.Event) -> None:
        if self.rate <= 0:
            return
        while not stop.is_set():
            now = self._clock()
            self._allowance = min(
                float(self.rate), self._allowance + (now - self._last) * self.rate
            )
            self._last = now
            if self._allowance >= size or self._allowance >= self.rate:
                self._allowance -= size
                return
            stop.wait((size - self._allowance) / self.rate)


ChannelFactory = Callable[[str], grpc.Channel]


class Uploader:
    def __init__(
        self,
        spool: Spool,
        target: str,
        channel: ChannelFactory,
        *,
        metadata: tuple[tuple[str, str], ...] = (),
        bytes_per_second: int = 0,
        label_grace_s: float = LABEL_GRACE_S,
        timeout_s: float = 300.0,
    ) -> None:
        self.spool = spool
        self.target = target
        self._channel = channel
        self._metadata = metadata
        self._throttle = _Throttle(bytes_per_second)
        self.label_grace_s = label_grace_s
        self.timeout_s = timeout_s
        self.counters: Counter[str] = Counter()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="neura-capture-upload", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout_s: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout_s)

    def nudge(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        backoff = 1.0
        channel = self._channel(self.target)
        stub = capture_pb2_grpc.CaptureStub(channel)
        try:
            while not self._stop.is_set():
                sent_any = False
                try:
                    sent_any = self.drain(stub)
                    backoff = 1.0
                except grpc.RpcError as exc:
                    self.counters["upload_failures"] += 1
                    log.warning(
                        "episode upload failed (%s); retrying in %.0f s", exc.code(), backoff
                    )
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 60.0)
                    continue
                self._wake.wait(0.2 if sent_any else 1.0)
                self._wake.clear()
        finally:
            channel.close()

    def drain(self, stub: capture_pb2_grpc.CaptureStub) -> bool:
        """Upload every episode that is ready. Returns whether any was sent."""
        sent = False
        now = time.time()
        for episode in self.spool.ready():
            if self._stop.is_set():
                break
            manifest_path = episode / MANIFEST
            if now - manifest_path.stat().st_mtime < self.label_grace_s:
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("capture_mode") == "failures" and not is_failure(manifest):
                self.spool.remove(episode)
                self.counters["not_failures"] += 1
                continue
            claimed = self.spool.claim(episode)
            if claimed is None:
                continue
            try:
                receipt = self._upload(stub, claimed)
            except BaseException:
                self.spool.release(claimed)
                raise
            self.spool.remove(claimed)
            self.counters["uploaded" if receipt.stored else "already_at_site"] += 1
            sent = True
        return sent

    def _upload(
        self, stub: capture_pb2_grpc.CaptureStub, episode: Path
    ) -> capture_pb2.EpisodeReceipt:
        manifest = json.loads((episode / MANIFEST).read_text(encoding="utf-8"))
        check_manifest(manifest)
        receipt: capture_pb2.EpisodeReceipt = stub.UploadEpisode(
            self._parts(episode, manifest), timeout=self.timeout_s, metadata=self._metadata
        )
        return receipt

    def _parts(
        self, episode: Path, manifest: dict[str, object]
    ) -> Iterator[capture_pb2.EpisodeUpload]:
        yield capture_pb2.EpisodeUpload(
            header=capture_pb2.EpisodeHeader(
                episode_id=str(manifest["episode_id"]), manifest=canonical(manifest)
            )
        )
        files = manifest["files"]
        assert isinstance(files, dict)
        for name in sorted(files):
            with (episode / name).open("rb") as handle:
                while block := handle.read(PART_BYTES):
                    self._throttle.wait(len(block), self._stop)
                    yield capture_pb2.EpisodeUpload(
                        data=capture_pb2.EpisodeData(file=name, data=block)
                    )
