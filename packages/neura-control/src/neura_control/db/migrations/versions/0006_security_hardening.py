"""security hardening

- Idempotency records now hold the response sealed with a key derived from the client's
  Idempotency-Key, under the key's hash (neura_control.api.common). Rows written before held the
  response in plaintext, including secrets shown once (new API tokens, enrollment tokens): they
  are deleted. A retry of a request from before the upgrade runs again.
- Audit records are append-only: a trigger refuses any UPDATE or DELETE, so neither the service
  nor anyone using its database role can rewrite history.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-15 16:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM idempotency_keys")
    op.execute(
        """
        CREATE FUNCTION neura_refuse_audit_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit records are append-only'
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_records_append_only
        BEFORE UPDATE OR DELETE ON audit_records
        FOR EACH ROW EXECUTE FUNCTION neura_refuse_audit_change()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_records_append_only ON audit_records")
    op.execute("DROP FUNCTION neura_refuse_audit_change()")
