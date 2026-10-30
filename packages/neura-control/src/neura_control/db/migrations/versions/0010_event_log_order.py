"""the change-event log in commit order, and how far it was pruned

- events.xid: the transaction that wrote the event (``pg_current_xact_id()``). An event's cursor
  is taken when it is written but seen only when its transaction commits, so the stream reads the
  log in transaction order and only as far as the oldest transaction still running
  (neura_control.services.event_log). Events written before this migration get its transaction:
  they keep their cursor order.
- events_pruned: the newest event deleted from the log, kept by a trigger on every delete. A
  cursor before it has expired.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-30 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column(
            "xid",
            sa.BigInteger(),
            server_default=sa.text("(pg_current_xact_id())::text::bigint"),
            nullable=False,
        ),
    )
    op.create_index("ix_events_project_xid_cursor", "events", ["project_id", "xid", "cursor"])
    op.create_index("ix_events_xid_cursor", "events", ["xid", "cursor"])
    op.create_table(
        "events_pruned",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("xid", sa.BigInteger(), nullable=False),
        sa.Column("cursor", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        """
        CREATE FUNCTION neura_note_pruned_events() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO events_pruned (id, xid, cursor)
            SELECT 1, gone.xid, gone.cursor FROM gone
            ORDER BY gone.xid DESC, gone.cursor DESC LIMIT 1
            ON CONFLICT (id) DO UPDATE SET xid = excluded.xid, cursor = excluded.cursor
            WHERE (events_pruned.xid, events_pruned.cursor) < (excluded.xid, excluded.cursor);
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER events_note_pruned AFTER DELETE ON events
        REFERENCING OLD TABLE AS gone
        FOR EACH STATEMENT EXECUTE FUNCTION neura_note_pruned_events()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER events_note_pruned ON events")
    op.execute("DROP FUNCTION neura_note_pruned_events()")
    op.drop_table("events_pruned")
    op.drop_index("ix_events_xid_cursor", table_name="events")
    op.drop_index("ix_events_project_xid_cursor", table_name="events")
    op.drop_column("events", "xid")
