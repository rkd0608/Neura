"""The change-event log, read in the order its transactions wrote it (design §20.6.1).

An event's cursor comes from a sequence when it is written, but the event can be seen only once its
transaction commits, and transactions commit in any order. A reader that went on from "the cursors
after the last one it saw" would skip an event whose transaction committed after a later cursor's.
So the log is read in the order of the transactions that wrote it (``xid``, then ``cursor``), and
only up to its fence: the oldest transaction still running (``pg_snapshot_xmin``). Every
transaction below the fence has committed or never will, so whatever becomes visible later sorts
after everything read so far.

A reader stands at an event: the last one it read, or a later one of another project (a stream
moves past other projects' events too, so a quiet project's cursor stays recent). That event's
cursor is what a client resumes from; 0 is the start of the log. The log keeps the newest event it
ever deleted (``events_pruned``, written by a trigger): a reader before it has expired, since an
event after its cursor may be gone.
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import BigInteger, Text, cast, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from neura_control.db.models import Event, EventsPruned

BATCH = 200


class Position(NamedTuple):
    """Where a reader stands, in the order the log is read."""

    xid: int
    cursor: int


START = Position(0, 0)


class Batch(NamedTuple):
    events: list[Event]
    position: Position  # where the reader stands after them


async def _fence(session: AsyncSession) -> int:
    xmin = cast(cast(func.pg_snapshot_xmin(func.pg_current_snapshot()), Text), BigInteger)
    return int(await session.scalar(select(xmin)) or 0)


async def _pruned(session: AsyncSession) -> Position:
    row = await session.get(EventsPruned, 1)
    return Position(row.xid, row.cursor) if row is not None else START


async def _newest(session: AsyncSession, fence: int) -> Position:
    """The newest event below the fence, or where the log was pruned to if that is later."""
    row = (
        await session.execute(
            select(Event.xid, Event.cursor)
            .where(Event.xid < fence)
            .order_by(Event.xid.desc(), Event.cursor.desc())
            .limit(1)
        )
    ).first()
    pruned = await _pruned(session)
    return max(Position(*row) if row else START, pruned if pruned.xid < fence else START)


async def now(session: AsyncSession) -> Position:
    """Where a reader that starts now stands: after every event it could already read."""
    return await _newest(session, await _fence(session))


async def resume(session: AsyncSession, cursor: int) -> Position | None:
    """Where a reader that got to ``cursor`` stands, or None when it has expired."""
    pruned = await _pruned(session)
    xid = await session.scalar(select(Event.xid).where(Event.cursor == cursor))
    if cursor == 0:
        position = START
    elif xid is not None:
        position = Position(xid, cursor)
    elif cursor == pruned.cursor:
        position = pruned  # the newest event deleted: nothing after it was
    else:
        return None
    return position if position >= pruned else None


async def read(session: AsyncSession, project_id: str, position: Position) -> Batch:
    """The project's events after ``position``, up to the fence, and where they leave the
    reader."""
    fence = await _fence(session)
    events = list(
        (
            await session.scalars(
                select(Event)
                .where(
                    Event.project_id == project_id,
                    Event.xid < fence,
                    tuple_(Event.xid, Event.cursor) > tuple_(position.xid, position.cursor),
                )
                .order_by(Event.xid, Event.cursor)
                .limit(BATCH)
            )
        ).all()
    )
    if len(events) == BATCH:
        return Batch(events, Position(events[-1].xid, events[-1].cursor))
    # Every event of the project below the fence is read: the reader stands at the newest one
    # below it, whichever project's.
    return Batch(events, max(position, await _newest(session, fence)))
