"""episode capture

- deployments.capture: what a deployment's robots record (design §28.2). Existing deployments
  record nothing.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17 21:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column(
            "capture",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("""'{"mode": "off"}'::jsonb"""),
        ),
    )
    op.alter_column("deployments", "capture", server_default=None)


def downgrade() -> None:
    op.drop_column("deployments", "capture")
