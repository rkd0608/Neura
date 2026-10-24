"""policies, rollouts, discovered robots, bench sites, activity, publisher keys, CLI sign-in

- policies and policy_versions: a trained model and its numbered versions, each a registered
  model artifact and the source it came from (design §29.1).
- rollouts: one change to what some robots run, and its steps (design §29.4).
- sites.kind: bench or production. Existing sites are production.
- robots.source and robots.hardware: robots the site's agent found (design §29.5).
- events: a one-line summary, a level and the site, robot and rollout an event is about, for the
  activity a person reads.
- publisher_keys: the keys a workspace signs artifacts with (public parts).
- login_sessions: the CLI signing in through the browser.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-23 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
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


def _jsonb(name: str, *, nullable: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), nullable=nullable)


def upgrade() -> None:
    op.add_column(
        "sites",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="production"),
    )
    op.alter_column("sites", "kind", server_default=None)
    op.add_column(
        "robots", sa.Column("source", sa.String(length=16), nullable=False, server_default="api")
    )
    op.alter_column("robots", "source", server_default=None)
    op.add_column(
        "robots",
        sa.Column(
            "hardware",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("robots", "hardware", server_default=None)
    for column in (
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("level", sa.String(length=16), nullable=True),
        sa.Column("site_id", sa.String(length=40), nullable=True),
        sa.Column("robot_id", sa.String(length=40), nullable=True),
        sa.Column("rollout_id", sa.String(length=40), nullable=True),
    ):
        op.add_column("events", column)

    op.create_table(
        "policies",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        _jsonb("labels"),
        *_resource(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name"),
    )
    op.create_index("ix_policies_project_id", "policies", ["project_id"])
    op.create_table(
        "policy_versions",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("policy_id", sa.String(length=40), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("model_artifact_id", sa.String(length=40), nullable=False),
        _jsonb("source"),
        sa.Column("notes", sa.Text(), nullable=False),
        *_resource(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["policy_id"], ["policies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["model_artifact_id"], ["contract_documents.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("policy_id", "number"),
        sa.UniqueConstraint("policy_id", "model_artifact_id"),
    )
    op.create_index("ix_policy_versions_project_id", "policy_versions", ["project_id"])
    op.create_index("ix_policy_versions_policy_id", "policy_versions", ["policy_id"])
    op.create_table(
        "rollouts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("project_id", sa.String(length=40), nullable=False),
        sa.Column("site_id", sa.String(length=40), nullable=False),
        sa.Column("deployment_id", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("policy_version_id", sa.String(length=40), nullable=True),
        _jsonb("robot_ids"),
        _jsonb("options"),
        sa.Column("state", sa.String(length=24), nullable=False),
        _jsonb("steps"),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("revision_id", sa.String(length=40), nullable=True),
        sa.Column("provisional_revision_id", sa.String(length=40), nullable=True),
        _jsonb("baseline_revision_ids"),
        sa.Column("evaluation_id", sa.String(length=40), nullable=True),
        sa.Column("operation_id", sa.String(length=40), nullable=True),
        sa.Column("approved_by", sa.String(length=256), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_resource(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["policy_version_id"], ["policy_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rollouts_project_id", "rollouts", ["project_id"])
    op.create_index("ix_rollouts_deployment_id", "rollouts", ["deployment_id"])
    op.create_index("ix_rollouts_operation_id", "rollouts", ["operation_id"])
    op.create_index("ix_rollouts_project_state", "rollouts", ["project_id", "state"])
    op.create_table(
        "publisher_keys",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("organization_id", sa.String(length=40), nullable=False),
        sa.Column("key_id", sa.String(length=256), nullable=False),
        sa.Column("public_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        *_resource(),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "key_id"),
    )
    op.create_index("ix_publisher_keys_organization_id", "publisher_keys", ["organization_id"])
    op.create_table(
        "login_sessions",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("user_code", sa.String(length=16), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("client_name", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("organization_id", sa.String(length=40), nullable=True),
        sa.Column("approved_by", sa.String(length=512), nullable=True),
        _jsonb("scopes"),
        sa.Column("api_token_id", sa.String(length=40), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_code"),
    )


def downgrade() -> None:
    op.drop_table("login_sessions")
    op.drop_table("publisher_keys")
    op.drop_table("rollouts")
    op.drop_table("policy_versions")
    op.drop_table("policies")
    for column in ("rollout_id", "robot_id", "site_id", "level", "summary"):
        op.drop_column("events", column)
    op.drop_column("robots", "hardware")
    op.drop_column("robots", "source")
    op.drop_column("sites", "kind")
