"""The site's episode store (design §28.2): every episode robots uploaded, on the site's disk.

Episodes are payload: they never leave the site (design §18.1). The cloud sees only their
metadata (counts, outcomes, sizes) through the agent's allowlisted reports.

Layout under the store's root::

    episodes/<episode_id>/   complete, verified episodes (``neura-episode/1``), read-only
    incoming/                uploads in progress
    index.db                 SQLite: one row per episode, and dataset snapshots

A dataset is an immutable snapshot of episodes chosen by a filter, identified by a digest over
its episodes' manifests: training and evaluation name the exact data they used, and episodes
in a dataset are kept by retention until the dataset is released.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from neura_runtime.capture.episode import (
    MANIFEST,
    Episode,
    EpisodeError,
    Outcome,
    OutcomeSource,
    canonical,
    check_manifest,
    open_episode,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    execution_profile_digest TEXT NOT NULL,
    robot_profile_digest TEXT NOT NULL,
    task_epoch INTEGER NOT NULL,
    part INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_s REAL NOT NULL,
    steps INTEGER NOT NULL,
    frames INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    end_reason TEXT NOT NULL,
    fault_reason TEXT,
    outcome TEXT NOT NULL,
    outcome_source TEXT NOT NULL,
    outcome_note TEXT NOT NULL DEFAULT '',
    instruction TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL,
    stored_at TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS episodes_by_revision ON episodes (revision_id, started_at);
CREATE INDEX IF NOT EXISTS episodes_by_artifact ON episodes (artifact_digest, started_at);
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    filter TEXT NOT NULL,
    digest TEXT NOT NULL,
    episodes INTEGER NOT NULL,
    steps INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    released INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dataset_episodes (
    dataset_id TEXT NOT NULL REFERENCES datasets (dataset_id),
    episode_id TEXT NOT NULL,
    PRIMARY KEY (dataset_id, episode_id)
);
"""

COLUMNS = (
    "episode_id",
    "robot_id",
    "revision_id",
    "artifact_digest",
    "execution_profile_digest",
    "robot_profile_digest",
    "task_epoch",
    "part",
    "started_at",
    "ended_at",
    "duration_s",
    "steps",
    "frames",
    "complete",
    "end_reason",
    "fault_reason",
    "outcome",
    "outcome_source",
    "outcome_note",
    "instruction",
    "bytes",
    "stored_at",
)


class StoreError(ValueError):
    """A request the store refuses; ``code`` is a reason code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000):012x}{secrets.token_hex(6)}"


@dataclass(frozen=True, slots=True)
class EpisodeFilter:
    """Which episodes: every field narrows. Outcomes and end reasons are any-of."""

    robot_ids: tuple[str, ...] = ()
    revision_ids: tuple[str, ...] = ()
    artifact_digests: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ()
    end_reasons: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None
    complete_only: bool = True
    limit: int | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> EpisodeFilter:
        def strings(key: str) -> tuple[str, ...]:
            values = data.get(key) or []
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise StoreError("CAPTURE_FILTER_INVALID", f"{key} must be a list of strings")
            return tuple(values)

        limit = data.get("limit")
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            raise StoreError("CAPTURE_FILTER_INVALID", "limit must be a positive integer")
        return cls(
            robot_ids=strings("robot_ids"),
            revision_ids=strings("revision_ids"),
            artifact_digests=strings("artifact_digests"),
            outcomes=strings("outcomes"),
            end_reasons=strings("end_reasons"),
            since=data.get("since"),
            until=data.get("until"),
            complete_only=bool(data.get("complete_only", True)),
            limit=limit,
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"complete_only": self.complete_only}
        for key in ("robot_ids", "revision_ids", "artifact_digests", "outcomes", "end_reasons"):
            if getattr(self, key):
                out[key] = list(getattr(self, key))
        for key in ("since", "until", "limit"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out

    def where(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        for column, values in (
            ("robot_id", self.robot_ids),
            ("revision_id", self.revision_ids),
            ("artifact_digest", self.artifact_digests),
            ("outcome", self.outcomes),
            ("end_reason", self.end_reasons),
        ):
            if values:
                clauses.append(f"{column} IN ({','.join('?' * len(values))})")
                args.extend(values)
        if self.since is not None:
            clauses.append("started_at >= ?")
            args.append(self.since)
        if self.until is not None:
            clauses.append("started_at < ?")
            args.append(self.until)
        if self.complete_only:
            clauses.append("complete = 1")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


class EpisodeStore:
    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = 200 * 1024**3,
        max_age_days: int | None = None,
        max_episode_bytes: int = 16 * 1024**3,
    ) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.max_age_days = max_age_days
        self.max_episode_bytes = max_episode_bytes
        (root / "episodes").mkdir(parents=True, exist_ok=True)
        incoming = root / "incoming"
        shutil.rmtree(incoming, ignore_errors=True)  # uploads cut short by a restart
        incoming.mkdir()
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.root / "index.db", timeout=30.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def path(self, episode_id: str) -> Path:
        if not episode_id.startswith("ep_") or not episode_id.replace("_", "").isalnum():
            raise StoreError("CAPTURE_EPISODE_UNKNOWN", f"no episode {episode_id!r}")
        return self.root / "episodes" / episode_id

    # --- ingest -----------------------------------------------------------------------------

    def has(self, episode_id: str) -> bool:
        with self._db() as db:
            row = db.execute(
                "SELECT 1 FROM episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        return row is not None

    def begin(self, manifest_bytes: bytes, *, robot_id: str) -> Upload:
        """Start receiving an episode from ``robot_id`` (the authenticated robot)."""
        try:
            manifest = json.loads(manifest_bytes)
            check_manifest(manifest)
        except (ValueError, EpisodeError) as exc:
            raise StoreError("CAPTURE_EPISODE_INVALID", str(exc)) from exc
        if manifest["robot_id"] != robot_id:
            raise StoreError(
                "CAPTURE_ROBOT_MISMATCH",
                f"robot {robot_id} uploaded an episode recorded by {manifest['robot_id']}",
            )
        declared = sum(entry["bytes"] for entry in manifest["files"].values())
        if declared > self.max_episode_bytes:
            raise StoreError("CAPTURE_EPISODE_TOO_LARGE", f"{declared} bytes")
        directory = self.root / "incoming" / f"{manifest['episode_id']}-{secrets.token_hex(4)}"
        directory.mkdir()
        return Upload(self, manifest, directory)

    def _commit(self, upload: Upload) -> bool:
        manifest = upload.manifest
        episode_id = manifest["episode_id"]
        target = self.path(episode_id)
        if self.has(episode_id):
            shutil.rmtree(upload.directory, ignore_errors=True)
            return False
        (upload.directory / MANIFEST).write_bytes(canonical(manifest))
        try:
            open_episode(upload.directory, verify=True)
        except EpisodeError as exc:
            shutil.rmtree(upload.directory, ignore_errors=True)
            raise StoreError("CAPTURE_EPISODE_INVALID", str(exc)) from exc
        for file in upload.directory.iterdir():
            file.chmod(0o444)
        try:
            upload.directory.rename(target)
        except OSError:
            # The same episode, uploaded twice at once: the other upload stored it.
            _remove(upload.directory)
            return False
        size = sum(entry["bytes"] for entry in manifest["files"].values())
        end = manifest["end"]
        outcome = manifest["outcome"]
        row = {
            "episode_id": episode_id,
            "robot_id": manifest["robot_id"],
            "revision_id": manifest["revision_id"],
            "artifact_digest": manifest["artifact_digest"],
            "execution_profile_digest": manifest["execution_profile_digest"],
            "robot_profile_digest": manifest["robot_profile_digest"],
            "task_epoch": int(manifest.get("task_epoch", 0)),
            "part": int(manifest.get("part", 0)),
            "started_at": str(manifest.get("started_at", "")),
            "ended_at": str(manifest.get("ended_at", "")),
            "duration_s": float(manifest.get("duration_s", 0.0)),
            "steps": int(manifest["steps"]),
            "frames": int(manifest.get("frames", 0)),
            "complete": int(bool(manifest.get("complete", False))),
            "end_reason": end["reason"],
            "fault_reason": end.get("fault_reason"),
            "outcome": outcome["label"],
            "outcome_source": outcome.get("source", OutcomeSource.ROBOT.value),
            "outcome_note": str(outcome.get("note", ""))[:500],
            "instruction": str(manifest.get("instruction", ""))[:1000],
            "bytes": size,
            "stored_at": _now(),
            "manifest_sha256": hashlib.sha256(canonical(manifest)).hexdigest(),
        }
        with self._db() as db:
            db.execute(
                f"INSERT INTO episodes ({','.join(row)}) VALUES ({','.join('?' * len(row))})",  # noqa: S608 - fixed columns
                tuple(row.values()),
            )
        return True

    # --- reading ------------------------------------------------------------------------------

    def find(self, query: EpisodeFilter | None = None, *, offset: int = 0) -> list[dict[str, Any]]:
        query = query or EpisodeFilter(complete_only=False)
        where, args = query.where()
        limit = f" LIMIT {int(query.limit)} OFFSET {int(offset)}" if query.limit else ""
        with self._db() as db:
            # Fixed columns and clauses; every value is a bound parameter.
            sql = f"SELECT {','.join(COLUMNS)} FROM episodes{where}"  # noqa: S608
            rows = db.execute(sql + " ORDER BY started_at, episode_id" + limit, args).fetchall()
        return [_row(r) for r in rows]

    def get(self, episode_id: str) -> dict[str, Any]:
        with self._db() as db:
            sql = f"SELECT {','.join(COLUMNS)} FROM episodes WHERE episode_id = ?"  # noqa: S608
            row = db.execute(sql, (episode_id,)).fetchone()
        if row is None:
            raise StoreError("CAPTURE_EPISODE_UNKNOWN", f"no episode {episode_id}")
        return _row(row)

    def episode(self, episode_id: str) -> Episode:
        """The episode on disk, with its manifest as last labeled."""
        record = self.get(episode_id)
        episode = open_episode(self.path(episode_id), verify=False)
        outcome = {
            "label": record["outcome"],
            "source": record["outcome_source"],
            **({"note": record["outcome_note"]} if record["outcome_note"] else {}),
        }
        return Episode(episode.directory, {**episode.manifest, "outcome": outcome})

    def summary(self) -> dict[str, Any]:
        """What the site holds, for the agent's report: no payload."""
        with self._db() as db:
            totals = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes),0), COALESCE(SUM(steps),0),"
                " MIN(started_at), MAX(started_at) FROM episodes"
            ).fetchone()
            outcomes = db.execute(
                "SELECT outcome, COUNT(*) FROM episodes GROUP BY outcome"
            ).fetchall()
            by_revision = db.execute(
                "SELECT revision_id, outcome, COUNT(*) FROM episodes GROUP BY revision_id, outcome"
            ).fetchall()
            datasets = db.execute("SELECT COUNT(*) FROM datasets WHERE released = 0").fetchone()
        revisions: dict[str, dict[str, int]] = {}
        for revision_id, outcome, count in by_revision:
            revisions.setdefault(revision_id, {})[outcome] = count
        return {
            "episodes": totals[0],
            "bytes": totals[1],
            "steps": totals[2],
            "oldest": totals[3],
            "newest": totals[4],
            "outcomes": dict(outcomes),
            "revisions": revisions,
            "datasets": datasets[0],
        }

    # --- changes ------------------------------------------------------------------------------

    def label(
        self,
        episode_id: str,
        outcome: str,
        *,
        source: str = OutcomeSource.OPERATOR.value,
        note: str = "",
    ) -> dict[str, Any]:
        if outcome not in {o.value for o in Outcome}:
            raise StoreError("CAPTURE_LABEL_INVALID", "outcome is success, failure or unknown")
        with self._db() as db:
            changed = db.execute(
                "UPDATE episodes SET outcome = ?, outcome_source = ?, outcome_note = ?"
                " WHERE episode_id = ?",
                (outcome, source, note[:500], episode_id),
            ).rowcount
        if not changed:
            raise StoreError("CAPTURE_EPISODE_UNKNOWN", f"no episode {episode_id}")
        return self.get(episode_id)

    def delete(self, episode_id: str) -> None:
        """Delete an episode, unless a dataset in use holds it."""
        with self._db() as db:
            pinned = db.execute(
                "SELECT d.dataset_id FROM dataset_episodes e JOIN datasets d USING (dataset_id)"
                " WHERE e.episode_id = ? AND d.released = 0",
                (episode_id,),
            ).fetchone()
            if pinned is not None:
                raise StoreError(
                    "CAPTURE_EPISODE_IN_DATASET",
                    f"{episode_id} is in dataset {pinned[0]}: release the dataset first",
                )
            if not db.execute("DELETE FROM episodes WHERE episode_id = ?", (episode_id,)).rowcount:
                raise StoreError("CAPTURE_EPISODE_UNKNOWN", f"no episode {episode_id}")
        _remove(self.path(episode_id))

    def enforce_retention(self, *, now: datetime | None = None) -> list[str]:
        """Delete the oldest episodes no dataset in use holds, until the store is within its
        size and age limits. Returns the deleted ids."""
        now = now or datetime.now(UTC)
        cutoff = (
            (now - timedelta(days=self.max_age_days)).isoformat() if self.max_age_days else None
        )
        with self._db() as db:
            total = db.execute("SELECT COALESCE(SUM(bytes),0) FROM episodes").fetchone()[0]
            candidates = db.execute(
                "SELECT episode_id, bytes, started_at FROM episodes WHERE episode_id NOT IN ("
                " SELECT e.episode_id FROM dataset_episodes e JOIN datasets d USING (dataset_id)"
                " WHERE d.released = 0) ORDER BY started_at, episode_id"
            ).fetchall()
        deleted = []
        for episode_id, size, started_at in candidates:
            too_old = cutoff is not None and started_at < cutoff
            if total <= self.max_bytes and not too_old:
                break
            with self._db() as db:
                db.execute("DELETE FROM episodes WHERE episode_id = ?", (episode_id,))
            _remove(self.path(episode_id))
            total -= size
            deleted.append(episode_id)
        return deleted

    # --- datasets -------------------------------------------------------------------------------

    def create_dataset(self, name: str, query: EpisodeFilter) -> dict[str, Any]:
        """Snapshot the episodes the filter selects; they are kept until the dataset is
        released."""
        episodes = self.find(query)
        if not episodes:
            raise StoreError("CAPTURE_DATASET_EMPTY", "no episode matches the filter")
        dataset_id = _new_id("ds")
        with self._db() as db:
            manifests = {
                row[0]: row[1]
                for row in db.execute(
                    "SELECT episode_id, manifest_sha256 FROM episodes WHERE episode_id IN"  # noqa: S608
                    f" ({','.join('?' * len(episodes))})",
                    [e["episode_id"] for e in episodes],
                ).fetchall()
            }
            # The digest names the data exactly: each episode's manifest (its files' digests)
            # and the outcome label it carried when the snapshot was taken.
            content = [
                {
                    "episode_id": e["episode_id"],
                    "manifest_sha256": manifests[e["episode_id"]],
                    "outcome": e["outcome"],
                }
                for e in episodes
            ]
            digest = "sha256:" + hashlib.sha256(canonical({"episodes": content})).hexdigest()
            record = {
                "dataset_id": dataset_id,
                "name": name[:200],
                "created_at": _now(),
                "filter": json.dumps(query.to_json(), sort_keys=True),
                "digest": digest,
                "episodes": len(episodes),
                "steps": sum(e["steps"] for e in episodes),
                "bytes": sum(e["bytes"] for e in episodes),
            }
            columns = ",".join(record)
            db.execute(
                f"INSERT INTO datasets ({columns}) VALUES ({','.join('?' * len(record))})",  # noqa: S608
                tuple(record.values()),
            )
            db.executemany(
                "INSERT INTO dataset_episodes (dataset_id, episode_id) VALUES (?, ?)",
                [(dataset_id, e["episode_id"]) for e in episodes],
            )
        return self.dataset(dataset_id)

    def dataset(self, dataset_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()
            if row is None:
                raise StoreError("CAPTURE_DATASET_UNKNOWN", f"no dataset {dataset_id}")
            members = [
                r[0]
                for r in db.execute(
                    "SELECT episode_id FROM dataset_episodes WHERE dataset_id = ?"
                    " ORDER BY episode_id",
                    (dataset_id,),
                ).fetchall()
            ]
        record = dict(row)
        record["filter"] = json.loads(record["filter"])
        record["released"] = bool(record["released"])
        record["episode_ids"] = members
        return record

    def datasets(self) -> list[dict[str, Any]]:
        with self._db() as db:
            ids = [r[0] for r in db.execute("SELECT dataset_id FROM datasets ORDER BY created_at")]
        return [self.dataset(i) for i in ids]

    def release_dataset(self, dataset_id: str) -> dict[str, Any]:
        with self._db() as db:
            if not db.execute(
                "UPDATE datasets SET released = 1 WHERE dataset_id = ?", (dataset_id,)
            ).rowcount:
                raise StoreError("CAPTURE_DATASET_UNKNOWN", f"no dataset {dataset_id}")
        return self.dataset(dataset_id)

    def dataset_episodes(self, dataset_id: str) -> list[Episode]:
        """The dataset's episodes, as they were when it was taken (labels included)."""
        record = self.dataset(dataset_id)
        if record["released"]:
            raise StoreError("CAPTURE_DATASET_RELEASED", f"dataset {dataset_id} was released")
        return [self.episode(e) for e in record["episode_ids"]]


def _row(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["complete"] = bool(record["complete"])
    return record


def _remove(directory: Path) -> None:
    if directory.exists():
        for file in directory.iterdir():
            with contextlib.suppress(OSError):
                file.chmod(0o644)
        shutil.rmtree(directory, ignore_errors=True)


class Upload:
    """An episode arriving: every file in order, checked against the manifest's sizes as it
    comes, and verified against its digests before it is stored."""

    def __init__(self, store: EpisodeStore, manifest: dict[str, Any], directory: Path) -> None:
        self.store = store
        self.manifest = manifest
        self.directory = directory
        self._files: dict[str, int] = {}
        self._current: str | None = None
        self._handle: Any = None
        self._done: set[str] = set()

    def write(self, name: str, data: bytes) -> None:
        files = self.manifest["files"]
        if name not in files:
            self.abort()
            raise StoreError("CAPTURE_EPISODE_INVALID", f"{name!r} is not in the manifest")
        if name != self._current:
            if name in self._done or name in self._files:
                self.abort()
                raise StoreError("CAPTURE_EPISODE_INVALID", f"{name} arrived out of order")
            self._close_current()
            self._current = name
            self._files[name] = 0
            self._handle = (self.directory / name).open("wb")
        written = self._files[name] + len(data)
        if written > files[name]["bytes"]:
            self.abort()
            raise StoreError("CAPTURE_EPISODE_INVALID", f"{name} is larger than its manifest says")
        self._handle.write(data)
        self._files[name] = written

    def _close_current(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            self._handle = None
        if self._current is not None:
            self._done.add(self._current)
        self._current = None

    def commit(self) -> bool:
        """Verify and store. Returns False when the site already held the episode."""
        self._close_current()
        return self.store._commit(self)

    def abort(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
            self._handle = None
        shutil.rmtree(self.directory, ignore_errors=True)
