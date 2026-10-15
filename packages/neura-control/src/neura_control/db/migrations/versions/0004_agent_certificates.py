"""agent certificates

Each server records the TLS client certificates the control plane's agent CA issued it (SHA-256
fingerprint and expiry), so the stream accepts only those (design §17.3, mutual TLS).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column(
            "agent_certificates",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("servers", "agent_certificates")
