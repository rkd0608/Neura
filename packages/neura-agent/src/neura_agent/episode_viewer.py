"""The site's episode viewer (design §28.2): what a robot recorded, for the site's operator to
watch and label. It is served by the agent's local API, behind the operator credential, and
nothing it shows leaves the site.

The page (``assets/episodes.*``) is static: its data comes from the local API, frames as PNG
images and steps as JSON.
"""

from __future__ import annotations

import struct
import zlib
from importlib import resources
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_runtime.capture.episode import Episode

# Steps sent for one episode at most: enough for a trace, small enough for a page.
MAX_ROWS = 2_000
# The page's own files, with what they are.
ASSETS = {
    "episodes": ("episodes.html", "text/html; charset=utf-8"),
    "episodes.js": ("episodes.js", "text/javascript; charset=utf-8"),
    "episodes.css": ("episodes.css", "text/css; charset=utf-8"),
}
# The page loads only itself, and images it made from frames it fetched with the credential.
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def asset(name: str) -> tuple[bytes, str]:
    filename, media_type = ASSETS[name]
    return resources.files("neura_agent").joinpath("assets", filename).read_bytes(), media_type


def png(frame: NDArray[np.uint8]) -> bytes:
    """An HxWx3 RGB frame as a PNG image (no filter, fast compression)."""
    height, width, _ = frame.shape
    rows = np.ascontiguousarray(frame, dtype=np.uint8).reshape(height, width * 3)
    raw = np.concatenate([np.zeros((height, 1), np.uint8), rows], axis=1).tobytes()

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 3))
        + chunk(b"IEND", b"")
    )


def frame_png(episode: Episode, role: str, index: int) -> bytes:
    """One recorded frame of a camera, as RGB whatever the camera's color order. Raises
    LookupError for a camera or frame the episode does not have."""
    camera = next((c for c in episode.manifest.get("cameras", []) if c["role"] == role), None)
    if camera is None:
        raise LookupError(f"the episode has no camera {role!r}")
    count = len(episode.steps().frame_offsets.get(role, []))
    if not 0 <= index < count:
        raise LookupError(f"camera {role!r} has frames 0 to {count - 1}")
    frame = episode.frame(role, index)
    if camera.get("color_order") == "bgr":
        frame = frame[..., ::-1]
    return png(frame)


def _rows(values: NDArray[np.float64]) -> list[list[float | None]]:
    return [[None if not np.isfinite(v) else round(float(v), 5) for v in row] for row in values]


def steps_json(episode: Episode, max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """The episode's steps for the page: time, commands and state, and the camera frame shown
    then, every nth step so at most ``max_rows`` are sent."""
    steps = episode.steps()
    manifest = episode.manifest
    total = len(steps.t_us)
    every = max(1, -(-total // max_rows))
    picked = slice(None, None, every)
    return {
        "episode_id": episode.episode_id,
        "steps": total,
        "every": every,
        "control_period_us": manifest.get("control_period_us"),
        "command_names": manifest.get("command_names", []),
        "state_names": manifest.get("state_names", []),
        "cameras": [
            {
                "role": c["role"],
                "width": c["width"],
                "height": c["height"],
                "frames": len(steps.frame_offsets.get(c["role"], [])),
            }
            for c in manifest.get("cameras", [])
        ],
        "t_s": [round(int(t) / 1e6, 4) for t in steps.t_us[picked]],
        "action": _rows(steps.action[picked]),
        "state": _rows(steps.state[picked]),
        # The latest camera frame at each step (frames are recorded every few steps): -1 before
        # the first.
        "frame": [int(f) for f in np.maximum.accumulate(steps.frame)[picked]] if total else [],
    }
