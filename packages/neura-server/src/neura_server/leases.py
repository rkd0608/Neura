"""Robot leases: who may be served (design §6.1).

At most one live lease per robot. Every grant increments a per-robot generation that is persisted
before the grant is returned, so generations stay monotonic across gateway restarts and a message
from an older lease can always be recognized and refused.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from neura_contracts import ReasonCode
from neura_contracts.identifiers import ulid


class GenerationStore(Protocol):
    def next_generation(self, robot_id: str) -> int:
        """Durably increment and return the robot's lease generation."""
        ...


class MemoryGenerationStore:
    def __init__(self) -> None:
        self._generations: dict[str, int] = {}

    def next_generation(self, robot_id: str) -> int:
        value = self._generations.get(robot_id, 0) + 1
        self._generations[robot_id] = value
        return value


class SqliteGenerationStore:
    """Durable store. The increment is committed (fsync) before it is returned."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS lease_generations ("
            " robot_id TEXT PRIMARY KEY, generation INTEGER NOT NULL)"
        )

    def next_generation(self, robot_id: str) -> int:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT INTO lease_generations (robot_id, generation) VALUES (?, 1) "
                    "ON CONFLICT(robot_id) DO UPDATE SET generation = generation + 1",
                    (robot_id,),
                )
                row = self._db.execute(
                    "SELECT generation FROM lease_generations WHERE robot_id = ?", (robot_id,)
                ).fetchone()
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return int(row[0])

    def close(self) -> None:
        self._db.close()


@dataclass(slots=True)
class Lease:
    robot_id: str
    session_id: str
    generation: int
    expires_at_us: int  # gateway clock


@dataclass(frozen=True, slots=True)
class LeaseDenied:
    reason: ReasonCode
    holder_session_id: str | None = None


class LeaseTable:
    def __init__(self, store: GenerationStore, ttl_us: int) -> None:
        if ttl_us <= 0:
            raise ValueError("lease ttl must be positive")
        self._store = store
        self.ttl_us = ttl_us
        self._by_robot: dict[str, Lease] = {}
        self._by_session: dict[str, Lease] = {}

    def acquire(self, robot_id: str, now_us: int) -> Lease | LeaseDenied:
        current = self._by_robot.get(robot_id)
        if current is not None:
            if current.expires_at_us > now_us:
                return LeaseDenied(ReasonCode.LEASE_BUSY, current.session_id)
            self._remove(current)
        generation = self._store.next_generation(robot_id)
        lease = Lease(
            robot_id=robot_id,
            session_id=f"ses_{ulid()}",
            generation=generation,
            expires_at_us=now_us + self.ttl_us,
        )
        self._by_robot[robot_id] = lease
        self._by_session[lease.session_id] = lease
        return lease

    def check(self, session_id: str, generation: int, now_us: int) -> Lease | LeaseDenied:
        lease = self._by_session.get(session_id)
        if lease is None:
            return LeaseDenied(ReasonCode.SESSION_UNKNOWN)
        if lease.generation != generation:
            return LeaseDenied(ReasonCode.LEASE_GENERATION_STALE)
        if lease.expires_at_us <= now_us:
            self._remove(lease)
            return LeaseDenied(ReasonCode.LEASE_EXPIRED)
        return lease

    def renew(self, session_id: str, generation: int, now_us: int) -> Lease | LeaseDenied:
        result = self.check(session_id, generation, now_us)
        if isinstance(result, Lease):
            result.expires_at_us = now_us + self.ttl_us
        return result

    def release(self, session_id: str) -> Lease | None:
        lease = self._by_session.get(session_id)
        if lease is not None:
            self._remove(lease)
        return lease

    def revoke(self, robot_id: str) -> Lease | None:
        """Operator revocation: the next open for this robot gets a new generation."""
        lease = self._by_robot.get(robot_id)
        if lease is not None:
            self._remove(lease)
        return lease

    def expire(self, now_us: int) -> list[Lease]:
        expired = [lease for lease in self._by_robot.values() if lease.expires_at_us <= now_us]
        for lease in expired:
            self._remove(lease)
        return expired

    def get(self, session_id: str) -> Lease | None:
        return self._by_session.get(session_id)

    def active(self) -> list[Lease]:
        return list(self._by_robot.values())

    def _remove(self, lease: Lease) -> None:
        self._by_robot.pop(lease.robot_id, None)
        self._by_session.pop(lease.session_id, None)
