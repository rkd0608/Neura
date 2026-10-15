"""artifact trust

- Publisher signatures on model artifacts (design §17.3): kept next to the immutable document, so
  adding one never changes its digest, and carried to sites in the manifest.
- Neura's registry of sealed weights (design §18.2): ciphertext only, opened at the site with the
  customer's key.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15 15:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contract_documents",
        sa.Column(
            "signatures",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_table(
        "sealed_weights",
        sa.Column("project_id", sa.String(40), nullable=False),
        sa.Column("digest", sa.String(71), nullable=False),
        sa.Column("key_id", sa.String(256), nullable=False),
        sa.Column("plaintext_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sealed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sealed_sha256", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "digest"),
    )


def downgrade() -> None:
    op.drop_table("sealed_weights")
    op.drop_column("contract_documents", "signatures")
