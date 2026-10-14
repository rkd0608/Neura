"""Async engine and session factory. URLs use the psycopg (v3) driver."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def sqlalchemy_url(database_url: str) -> str:
    """Map ``postgresql://`` to SQLAlchemy's psycopg dialect."""
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql+psycopg://" + database_url[len(prefix) :]
    raise ValueError("database_url must be a postgresql:// URL")


def create_engine(database_url: str, *, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(
        sqlalchemy_url(database_url),
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
