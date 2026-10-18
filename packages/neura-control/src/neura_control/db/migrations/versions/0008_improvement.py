"""evaluations, training jobs and release gates

- evaluations: a candidate revision evaluated against the one it would replace, at the site
  (design §28.3).
- training_jobs: a fine-tune of a revision's model on the episodes its site recorded, and what it
  produced (design §28.4).
- deployments.release_gates: evaluations a revision needs before it replaces the one robots run.
  Existing deployments need none.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-18 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _resource() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.String(length=256), nullable=False),
    ]


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column(
            "release_gates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("deployments", "release_gates", server_default=None)
    op.create_table(
        "evaluations",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("deployment_id", sa.String(length=40), nullable=False),
        sa.Column("candidate_revision_id", sa.String(length=40), nullable=False),
        sa.Column("baseline_revision_id", sa.String(length=40), nullable=True),
        sa.Column("suite", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("operation_id", sa.String(length=40), nullable=True),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=True),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_resource(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_revision_id"], ["revisions.id"]),
        sa.ForeignKeyConstraint(["baseline_revision_id"], ["revisions.id"]),
        sa.ForeignKeyConstraint(["operation_id"], ["operations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evaluations_project_id", "evaluations", ["project_id"])
    op.create_index("ix_evaluations_deployment_id", "evaluations", ["deployment_id"])
    op.create_index(
        "ix_evaluations_candidate_revision_id", "evaluations", ["candidate_revision_id"]
    )
    op.create_table(
        "training_jobs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("deployment_id", sa.String(length=40), nullable=False),
        sa.Column("base_revision_id", sa.String(length=40), nullable=False),
        sa.Column("episodes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recipe", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("artifact_name", sa.String(length=63), nullable=False),
        sa.Column("artifact_version", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=40), nullable=True),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("artifact_id", sa.String(length=40), nullable=True),
        sa.Column("execution_profile_id", sa.String(length=40), nullable=True),
        sa.Column("candidate_revision_id", sa.String(length=40), nullable=True),
        sa.Column("dataset_digest", sa.String(length=71), nullable=True),
        sa.Column("dataset_episodes", sa.Integer(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_resource(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["base_revision_id"], ["revisions.id"]),
        sa.ForeignKeyConstraint(["operation_id"], ["operations.id"]),
        sa.ForeignKeyConstraint(["artifact_id"], ["contract_documents.id"]),
        sa.ForeignKeyConstraint(["execution_profile_id"], ["contract_documents.id"]),
        sa.ForeignKeyConstraint(["candidate_revision_id"], ["revisions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_training_jobs_project_id", "training_jobs", ["project_id"])
    op.create_index("ix_training_jobs_deployment_id", "training_jobs", ["deployment_id"])


def downgrade() -> None:
    op.drop_table("training_jobs")
    op.drop_table("evaluations")
    op.drop_column("deployments", "release_gates")
