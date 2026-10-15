"""One actuator authority per robot (design §6.3).

Two layers, so a second process can never drive the same arm:

- an exclusive ``flock`` on a lock file named after the serial device (resolved through
  symlinks such as /dev/serial/by-id), which every Neura process takes before it connects;
- the serial port itself made exclusive (``TIOCEXCL``) once it is open, so any other program,
  Neura's or not, fails to open it.

``held()`` is what the runtime polls each control period: losing either layer faults the robot
with ACTUATOR_LOCK_LOST.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import termios
from pathlib import Path


class ActuatorBusyError(RuntimeError):
    """Another process holds this actuator."""


def default_lock_dir() -> Path:
    for candidate in (Path("/run/lock"), Path("/var/lock")):
        if candidate.is_dir() and os.access(candidate, os.W_OK):
            return candidate
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))  # noqa: S108 - fallback, per user


class ActuatorLock:
    def __init__(self, device: str, lock_dir: Path | None = None) -> None:
        self.device = os.path.realpath(device)
        name = self.device.strip("/").replace("/", "_") or "device"
        self.path = (lock_dir or default_lock_dir()) / f"neura-actuator-{name}.lock"
        self._fd: int | None = None
        self._serial_fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = ""
            with contextlib.suppress(OSError, ValueError):
                holder = f" (process {int(os.pread(fd, 32, 0).decode().strip())})"
            os.close(fd)
            raise ActuatorBusyError(
                f"{self.device} is driven by another process{holder}; one runtime per robot"
            ) from exc
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        self._fd = fd

    def make_port_exclusive(self, serial_fd: int) -> None:
        """Refuse every later open of the serial port (TIOCEXCL), whoever tries."""
        try:
            fcntl.ioctl(serial_fd, termios.TIOCEXCL)
        except OSError as exc:
            if exc.errno not in (errno.ENOTTY, errno.EINVAL):
                raise
            return  # not a terminal device (some test doubles): the flock still applies
        self._serial_fd = serial_fd

    def held(self) -> bool:
        if self._fd is None:
            return False
        try:
            # Still the same lock file: nobody removed it and took a new one.
            if os.fstat(self._fd).st_ino != os.stat(self.path).st_ino:
                return False
            if self._serial_fd is not None:
                os.fstat(self._serial_fd)  # the port is still open (not unplugged and closed)
        except OSError:
            return False
        return True

    def release(self) -> None:
        if self._serial_fd is not None:
            with contextlib.suppress(OSError):
                fcntl.ioctl(self._serial_fd, termios.TIOCNXCL)
            self._serial_fd = None
        if self._fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            self._fd = None

    def __enter__(self) -> ActuatorLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
