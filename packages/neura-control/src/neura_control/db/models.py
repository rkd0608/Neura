"""Control-plane schema (PostgreSQL).

Conventions:
- Primary keys are typed ULID strings (``ids.py``).
- ``version`` is an optimistic-concurrency counter exposed as the resource ETag.
- ``observed`` holds site-reported status. Only site reports write it; API clients never can.
- ``spec``-like columns are what clients ask for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Resource:
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    created_by: Mapped[str] = mapped_column(String(256), nullable=False, default="system")


class Organization(Resource, Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)


class Membership(Resource, Base):
    """A person in an organization. Added by SSO subject, or invited by email and bound to the
    subject the first time that person signs in with the verified email."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "subject", name="uq_memberships_org_subject"),
        UniqueConstraint("organization_id", "email", name="uq_memberships_org_email"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject: Mapped[str | None] = mapped_column(String(512), index=True)  # "<issuer>|<sub>"
    email: Mapped[str | None] = mapped_column(String(320), index=True)  # lower case
    role: Mapped[str] = mapped_column(String(32), nullable=False)


class ApiToken(Resource, Base):
    __tablename__ = "api_tokens"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    scopes: Mapped[list[Any]] = mapped_column(nullable=False)
    project_ids: Mapped[list[Any]] = mapped_column(nullable=False, default=list)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Project(Resource, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)


class Site(Resource, Base):
    __tablename__ = "sites"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    offline_authorization_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    # "bench": for trying things; its agent may approve activations itself if its operator also
    # installed it as a bench. "production": a person at the site approves every change.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="production")
    observed: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    # Desired-state manifest bookkeeping: the generation increments when the content digest changes.
    manifest_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    manifest_digest: Mapped[str | None] = mapped_column(String(71))


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    site_id: Mapped[str] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = _now()


class Server(Resource, Base):
    __tablename__ = "servers"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[str] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    allocated_gpu_indexes: Mapped[list[Any]] = mapped_column(nullable=False, default=list)
    observed: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    # Raw Ed25519 public key (hex) generated on the server at enrollment; the agent proves
    # possession of the private key on every connection.
    agent_public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # TLS client certificates the agent CA issued this server: [{"sha256", "not_after"}].
    agent_certificates: Mapped[list[Any]] = mapped_column(nullable=False, default=list)


class ContractDocument(Base):
    """Registered RobotProfile, ModelArtifact and ExecutionProfile documents (immutable)."""

    __tablename__ = "contract_documents"
    __table_args__ = (UniqueConstraint("project_id", "kind", "digest"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    digest: Mapped[str] = mapped_column(String(71), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    # Publisher signatures over a ModelArtifact's digest (neura_contracts.signing), sorted by key.
    signatures: Mapped[list[Any]] = mapped_column(nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    created_by: Mapped[str] = mapped_column(String(256), nullable=False)


class SealedWeights(Base):
    """A weight file in Neura's registry, sealed with the customer's key (design §18.2). The
    control plane stores the ciphertext only and cannot open it; sites fetch it by the plaintext
    digest an artifact lists."""

    __tablename__ = "sealed_weights"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    digest: Mapped[str] = mapped_column(String(71), primary_key=True)
    key_id: Mapped[str] = mapped_column(String(256), nullable=False)
    plaintext_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sealed_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sealed_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _now()
    created_by: Mapped[str] = mapped_column(String(256), nullable=False)


class Robot(Resource, Base):
    __tablename__ = "robots"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[str] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False
    )
    robot_profile_id: Mapped[str] = mapped_column(
        ForeignKey("contract_documents.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    # "api": created by a person or a script; "discovered": reported by the site's agent.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="api")
    # What the agent found: {"driver", "port", "serial", "simulated"}.
    hardware: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    observed: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)


class Policy(Resource, Base):
    """A trained model, whatever built it, with numbered versions (design §29.1)."""

    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    labels: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)


class PolicyVersion(Resource, Base):
    """One version of a policy: a registered model artifact and where it came from."""

    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint("policy_id", "number"),
        UniqueConstraint("policy_id", "model_artifact_id"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_id: Mapped[str] = mapped_column(
        ForeignKey("policies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    model_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("contract_documents.id", ondelete="RESTRICT"), nullable=False
    )
    # {"kind": "lerobot" | "python" | "container", "reference": "..."}
    source: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Rollout(Resource, Base):
    """One change to what some robots run: a deploy, a rollback or a try (design §29.4). Its
    steps advance on site reports; each step change is an event with a plain-language summary."""

    __tablename__ = "rollouts"
    __table_args__ = (Index("ix_rollouts_project_state", "project_id", "state"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version_id: Mapped[str | None] = mapped_column(ForeignKey("policy_versions.id"))
    robot_ids: Mapped[list[Any]] = mapped_column(nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    steps: Mapped[list[Any]] = mapped_column(nullable=False, default=list)
    reason: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # The revision robots switch to, once timed; the first one prepared before the timing.
    revision_id: Mapped[str | None] = mapped_column(String(40))
    provisional_revision_id: Mapped[str | None] = mapped_column(String(40))
    # What each robot ran when the rollout started: robot id -> revision id (or null).
    baseline_revision_ids: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    evaluation_id: Mapped[str | None] = mapped_column(String(40))
    operation_id: Mapped[str | None] = mapped_column(String(40), index=True)
    approved_by: Mapped[str | None] = mapped_column(String(256))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublisherKey(Resource, Base):
    """A key the workspace signs model artifacts with (public part only). Install commands carry
    them so a new site trusts them; a site never trusts a key it was not given locally."""

    __tablename__ = "publisher_keys"
    __table_args__ = (UniqueConstraint("organization_id", "key_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_id: Mapped[str] = mapped_column(String(256), nullable=False)
    public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)


class LoginSession(Base):
    """A CLI signing in through the browser (device authorization, RFC 8628 style)."""

    __tablename__ = "login_sessions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    client_name: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    organization_id: Mapped[str | None] = mapped_column(String(40))
    approved_by: Mapped[str | None] = mapped_column(String(512))
    scopes: Mapped[list[Any]] = mapped_column(nullable=False, default=list)
    api_token_id: Mapped[str | None] = mapped_column(String(40))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _now()


class Deployment(Resource, Base):
    __tablename__ = "deployments"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[str] = mapped_column(
        ForeignKey("sites.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    robot_ids: Mapped[list[Any]] = mapped_column(nullable=False)
    desired_revision_id: Mapped[str | None] = mapped_column(String(40))
    rollout: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    # What the deployment's robots record (design §28.2); {"mode": "off"} records nothing.
    capture: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    # Evaluations a revision needs before it replaces the one robots run (design §28.3).
    release_gates: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    labels: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    observed: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)


class Revision(Resource, Base):
    __tablename__ = "revisions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_artifact_id: Mapped[str] = mapped_column(
        ForeignKey("contract_documents.id"), nullable=False
    )
    execution_profile_id: Mapped[str] = mapped_column(
        ForeignKey("contract_documents.id"), nullable=False
    )
    robot_profile_id: Mapped[str] = mapped_column(
        ForeignKey("contract_documents.id"), nullable=False
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    revision_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    compatibility: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="Created")
    reason: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Site-computed capacity estimate (design §9.3), reported when the revision is prepared.
    capacity: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Operation(Resource, Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="Pending")
    reason: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)


class ActivationRequest(Resource, Base):
    __tablename__ = "activation_requests"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), nullable=False)
    robot_ids: Mapped[list[Any]] = mapped_column(nullable=False)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="AwaitingLocalApproval")
    approved_by: Mapped[str | None] = mapped_column(String(256))
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Evaluation(Resource, Base):
    """A candidate revision evaluated against the one it would replace, at the site (§28.3)."""

    __tablename__ = "evaluations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_revision_id: Mapped[str] = mapped_column(
        ForeignKey("revisions.id"), nullable=False, index=True
    )
    baseline_revision_id: Mapped[str | None] = mapped_column(ForeignKey("revisions.id"))
    suite: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    operation_id: Mapped[str | None] = mapped_column(ForeignKey("operations.id"))
    phase: Mapped[str] = mapped_column(String(16), nullable=False, default="Pending")
    reason: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verdict: Mapped[str | None] = mapped_column(String(16))
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrainingJob(Resource, Base):
    """A fine-tune of a revision's model on episodes its site recorded (design §28.4)."""

    __tablename__ = "training_jobs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    base_revision_id: Mapped[str] = mapped_column(ForeignKey("revisions.id"), nullable=False)
    episodes: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    recipe: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    artifact_name: Mapped[str] = mapped_column(String(63), nullable=False)
    artifact_version: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(ForeignKey("operations.id"))
    phase: Mapped[str] = mapped_column(String(16), nullable=False, default="Pending")
    reason: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # What the job produced, once the site's result verified.
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("contract_documents.id"))
    execution_profile_id: Mapped[str | None] = mapped_column(ForeignKey("contract_documents.id"))
    candidate_revision_id: Mapped[str | None] = mapped_column(ForeignKey("revisions.id"))
    dataset_digest: Mapped[str | None] = mapped_column(String(71))
    dataset_episodes: Mapped[int | None] = mapped_column(Integer)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SiteCommand(Base):
    """Outbox of commands for a site agent, delivered over its outbound stream (M4)."""

    __tablename__ = "site_commands"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    site_id: Mapped[str] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    command_id: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    operation_id: Mapped[str | None] = mapped_column(ForeignKey("operations.id"))
    command: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = _now()
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RobotSession(Base):
    __tablename__ = "robot_sessions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    robot_id: Mapped[str] = mapped_column(
        ForeignKey("robots.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(40), nullable=False)
    lease_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    metrics: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class Fault(Base):
    __tablename__ = "faults"
    __table_args__ = (
        UniqueConstraint("project_id", "site_fault_key", name="uq_faults_project_site_fault_key"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # Key the site assigned, so repeated reports of one fault are recorded once.
    site_fault_key: Mapped[str | None] = mapped_column(String(128))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    robot_id: Mapped[str] = mapped_column(
        ForeignKey("robots.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(String(40))
    revision_id: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    latched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    local_response: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_ids: Mapped[list[Any]] = mapped_column(nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class AuditRecord(Base):
    __tablename__ = "audit_records"
    __table_args__ = (Index("ix_audit_project_id", "project_id", "id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(512), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(80), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = _now()


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_project_cursor", "project_id", "cursor"),)
    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(40), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # For people (activity): one sentence, how much it matters, and what it is about.
    summary: Mapped[str | None] = mapped_column(Text)
    level: Mapped[str | None] = mapped_column(String(16))
    site_id: Mapped[str | None] = mapped_column(String(40))
    robot_id: Mapped[str | None] = mapped_column(String(40))
    rollout_id: Mapped[str | None] = mapped_column(String(40))
    occurred_at: Mapped[datetime] = _now()


class MetricPoint(Base):
    __tablename__ = "metric_points"
    __table_args__ = (Index("ix_metric_points_lookup", "project_id", "metric", "ts"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(40), nullable=False)
    site_id: Mapped[str] = mapped_column(String(40), nullable=False)
    robot_id: Mapped[str | None] = mapped_column(String(40))
    revision_id: Mapped[str | None] = mapped_column(String(40))
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float] = mapped_column(Double, nullable=False)


class SupportGrant(Resource, Base):
    __tablename__ = "support_grants"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    operations: Mapped[list[Any]] = mapped_column(nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    principal: Mapped[str] = mapped_column(String(512), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    response_headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
