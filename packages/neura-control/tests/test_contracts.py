"""Registering contract documents (services/contracts.py): by digest, once, even concurrently."""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conftest import contract
from neura_control.db.engine import create_engine, session_factory
from neura_control.db.models import Project
from neura_control.services import bootstrap, contracts
from neura_control.settings import ControlPlaneSettings


async def test_registering_one_document_twice_at_once_gives_one_row(
    settings: ControlPlaneSettings,
) -> None:
    engine = create_engine(str(settings.database_url), pool_size=3)
    sessions = session_factory(engine)
    document = contract("so101.robot.yaml")
    try:
        async with sessions.begin() as session:
            org = await bootstrap.ensure_organization(session, "Acme Robotics")
            session.add(Project(id="prj_a", organization_id=org.id, name="a", display_name="A"))
        async with sessions() as first, sessions() as second:
            row, new = await contracts.register(first, "prj_a", document, created_by="one")
            assert new
            pid = await second.scalar(text("SELECT pg_backend_pid()"))
            racing = asyncio.create_task(
                contracts.register(second, "prj_a", document, created_by="two")
            )
            await _until_waiting_for_a_lock(sessions, pid)
            await first.commit()
            again, new_again = await racing
            await second.commit()
        assert (again.id, new_again) == (row.id, False)
    finally:
        await engine.dispose()


async def _until_waiting_for_a_lock(sessions: async_sessionmaker[AsyncSession], pid: int) -> None:
    """Return once backend ``pid`` waits on a lock: the second insert is blocked on the first."""
    deadline = time.monotonic() + 10
    query = text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid")
    async with sessions() as watcher:
        while await watcher.scalar(query, {"pid": pid}) != "Lock":
            assert time.monotonic() < deadline, "the second registration never waited"
            await watcher.rollback()  # statistics are a snapshot per transaction
            await asyncio.sleep(0.01)
