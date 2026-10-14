"""Database connectivity checks. The schema and data access layer arrive in M3."""

from __future__ import annotations

import psycopg
from pydantic import PostgresDsn


async def database_reachable(url: PostgresDsn, timeout_s: float) -> bool:
    """Return True if a connection succeeds and a trivial query returns."""
    try:
        async with await psycopg.AsyncConnection.connect(
            str(url), connect_timeout=max(1, round(timeout_s))
        ) as conn:
            cursor = await conn.execute("SELECT 1")
            row = await cursor.fetchone()
            return row == (1,)
    except (psycopg.Error, OSError):
        return False
