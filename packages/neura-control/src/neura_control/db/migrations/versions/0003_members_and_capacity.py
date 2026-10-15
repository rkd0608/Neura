"""members and capacity

Memberships become resources (id, version) that can be invitations by email; revisions record the
site's capacity estimate.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from neura_contracts.identifiers import ulid

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("revisions", sa.Column("capacity", postgresql.JSONB(), nullable=True))

    op.add_column("memberships", sa.Column("id", sa.String(length=40), nullable=True))
    op.add_column("memberships", sa.Column("email", sa.String(length=320), nullable=True))
    op.add_column(
        "memberships", sa.Column("version", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column(
        "memberships",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "memberships",
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default="system"),
    )
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT organization_id, subject FROM memberships")).all()
    for organization_id, subject in rows:
        connection.execute(
            sa.text(
                "UPDATE memberships SET id = :id WHERE organization_id = :org AND subject = :sub"
            ),
            {"id": f"mbr_{ulid()}", "org": organization_id, "sub": subject},
        )
    op.drop_constraint("memberships_pkey", "memberships", type_="primary")
    op.alter_column("memberships", "id", nullable=False)
    op.create_primary_key("memberships_pkey", "memberships", ["id"])
    op.alter_column("memberships", "subject", existing_type=sa.String(length=512), nullable=True)
    for column in ("version", "created_by"):  # updated_at keeps now(), like every table
        op.alter_column("memberships", column, server_default=None)
    op.create_unique_constraint(
        "uq_memberships_org_subject", "memberships", ["organization_id", "subject"]
    )
    op.create_unique_constraint(
        "uq_memberships_org_email", "memberships", ["organization_id", "email"]
    )
    op.create_index("ix_memberships_organization_id", "memberships", ["organization_id"])
    op.create_index("ix_memberships_subject", "memberships", ["subject"])
    op.create_index("ix_memberships_email", "memberships", ["email"])


def downgrade() -> None:
    op.drop_index("ix_memberships_email", table_name="memberships")
    op.drop_index("ix_memberships_subject", table_name="memberships")
    op.drop_index("ix_memberships_organization_id", table_name="memberships")
    op.drop_constraint("uq_memberships_org_email", "memberships", type_="unique")
    op.drop_constraint("uq_memberships_org_subject", "memberships", type_="unique")
    op.execute("DELETE FROM memberships WHERE subject IS NULL")
    op.drop_constraint("memberships_pkey", "memberships", type_="primary")
    op.alter_column("memberships", "subject", existing_type=sa.String(length=512), nullable=False)
    op.create_primary_key("memberships_pkey", "memberships", ["organization_id", "subject"])
    for column in ("created_by", "updated_at", "version", "email", "id"):
        op.drop_column("memberships", column)
    op.drop_column("revisions", "capacity")
