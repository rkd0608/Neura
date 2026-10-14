"""Alembic environment: runs migrations synchronously with the psycopg driver."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from neura_control.db.engine import sqlalchemy_url
from neura_control.db.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=sqlalchemy_url(config.get_main_option("sqlalchemy.url") or ""),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = sqlalchemy_url(config.get_main_option("sqlalchemy.url") or "")
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
