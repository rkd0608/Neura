"""Request bodies, mirroring the request schemas in api/openapi.yaml.

Conformance tests validate every response against the spec, so responses are built as plain dicts
by ``serializers.py``; requests are parsed strictly here (unknown fields are rejected).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from neura_contracts.identifiers import ULID_PATTERN


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=200)]
LabelValue = Annotated[str, StringConstraints(max_length=256)]
Labels = Annotated[dict[Name, LabelValue], Field(max_length=64)]
SiteId = Annotated[str, StringConstraints(pattern=rf"^site_{ULID_PATTERN}$")]
RobotId = Annotated[str, StringConstraints(pattern=rf"^rbt_{ULID_PATTERN}$")]
ProjectId = Annotated[str, StringConstraints(pattern=rf"^prj_{ULID_PATTERN}$")]
RevisionId = Annotated[str, StringConstraints(pattern=rf"^rev_{ULID_PATTERN}$")]
RobotProfileId = Annotated[str, StringConstraints(pattern=rf"^rpf_{ULID_PATTERN}$")]
ArtifactId = Annotated[str, StringConstraints(pattern=rf"^art_{ULID_PATTERN}$")]
ExecutionProfileId = Annotated[str, StringConstraints(pattern=rf"^exp_{ULID_PATTERN}$")]


class ProjectSpec(Strict):
    name: Name
    display_name: DisplayName
    labels: Labels = Field(default_factory=dict)


class ProjectCreate(Strict):
    spec: ProjectSpec


class SiteSpec(Strict):
    name: Name
    display_name: DisplayName
    labels: Labels = Field(default_factory=dict)
    offline_authorization_hours: int = Field(ge=1, le=8760)


class SiteCreate(Strict):
    spec: SiteSpec


class EnrollmentTokenCreate(Strict):
    ttl_minutes: int | None = Field(default=None, ge=5, le=1440)


class ServerSpec(Strict):
    display_name: DisplayName
    labels: Labels = Field(default_factory=dict)
    allocated_gpu_indexes: list[Annotated[int, Field(ge=0)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.allocated_gpu_indexes)) != len(self.allocated_gpu_indexes):
            raise ValueError("allocated_gpu_indexes must be unique")
        return self


class RobotSpec(Strict):
    name: Name
    display_name: DisplayName
    site_id: SiteId
    robot_profile_id: RobotProfileId
    labels: Labels = Field(default_factory=dict)


class RobotCreate(Strict):
    spec: RobotSpec


class ContractCreate(Strict):
    document: dict[str, Any]
    signatures: list[dict[str, Any]] | None = Field(default=None, max_length=16)


class AgentUpgradeCreate(Strict):
    version: Annotated[
        str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$")
    ]
    bundle_url: Annotated[str, StringConstraints(pattern=r"^https://\S{1,2000}$")]
    bundle_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ArtifactSignatureCreate(Strict):
    key_id: str
    public_key: str
    signature: str


class CompatibilityCheckRequest(Strict):
    model_artifact_id: ArtifactId
    robot_profile_id: RobotProfileId
    execution_profile_id: ExecutionProfileId


class Rollout(Strict):
    strategy: Literal["all_at_task_boundary", "single_robot_canary"]
    canary_robot_id: RobotId | None = None

    @model_validator(mode="after")
    def _canary(self) -> Self:
        if (self.strategy == "single_robot_canary") != (self.canary_robot_id is not None):
            raise ValueError("canary_robot_id is required exactly for single_robot_canary")
        return self


class CaptureSettings(Strict):
    """What the deployment's robots record (design §28.2). Episodes stay at the site; its
    operator may allow less than a deployment asks."""

    mode: Literal["off", "failures", "all"] = "off"
    image_stride: int = Field(default=1, ge=1, le=100)
    cameras: list[Annotated[str, StringConstraints(min_length=1, max_length=63)]] = Field(
        default_factory=list, max_length=16
    )
    max_episode_seconds: int = Field(default=600, ge=1, le=86_400)
    spool_max_gib: float | None = Field(default=None, gt=0, le=10_000)
    upload_mbps: float | None = Field(default=None, gt=0, le=100_000)


class ReleaseGates(Strict):
    """Evaluations a revision needs before it replaces the one robots run (design §28.3)."""

    evaluations: list[Literal["simulation", "replay"]] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.evaluations)) != len(self.evaluations):
            raise ValueError("evaluations must be unique")
        return self


class DeploymentSpec(Strict):
    name: Name
    display_name: DisplayName
    site_id: SiteId
    robot_ids: list[RobotId] = Field(min_length=1)
    desired_revision_id: RevisionId | None = None
    rollout: Rollout
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    release_gates: ReleaseGates = Field(default_factory=ReleaseGates)
    labels: Labels = Field(default_factory=dict)

    @model_validator(mode="after")
    def _robots(self) -> Self:
        if len(set(self.robot_ids)) != len(self.robot_ids):
            raise ValueError("robot_ids must be unique")
        if (
            self.rollout.canary_robot_id is not None
            and self.rollout.canary_robot_id not in self.robot_ids
        ):
            raise ValueError("the canary robot must be one of robot_ids")
        return self


class DeploymentCreate(Strict):
    spec: DeploymentSpec


class RevisionSpec(Strict):
    model_artifact_id: ArtifactId
    execution_profile_id: ExecutionProfileId
    robot_profile_id: RobotProfileId
    notes: Annotated[str, StringConstraints(max_length=4000)] = ""


class RevisionCreate(Strict):
    spec: RevisionSpec


class ActivationRequestCreate(Strict):
    revision_id: RevisionId
    robot_ids: list[RobotId] | None = None
    note: Annotated[str, StringConstraints(max_length=2000)] = ""


class RollbackCreate(Strict):
    to_revision_id: RevisionId
    robot_ids: list[RobotId] | None = None
    note: Annotated[str, StringConstraints(max_length=2000)] = ""


# --- improvement (design §28.3, §28.4) ------------------------------------------------------


def _unique_items(values: list[Any], name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must be unique")


class EpisodeFilter(Strict):
    """Which recorded episodes. Every field narrows; lists are any-of."""

    robot_names: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=100
    )
    revision_ids: list[RevisionId] = Field(default_factory=list, max_length=100)
    outcomes: list[Literal["success", "failure", "unknown"]] = Field(default_factory=list)
    end_reasons: list[
        Literal["stopped", "takeover", "fault", "new_epoch", "max_duration", "disconnected"]
    ] = Field(default_factory=list)
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    complete_only: bool = True
    limit: int | None = Field(default=None, ge=1, le=100_000)

    @model_validator(mode="after")
    def _lists(self) -> Self:
        _unique_items(self.outcomes, "outcomes")
        _unique_items(self.end_reasons, "end_reasons")
        if self.since is not None and self.until is not None and self.until <= self.since:
            raise ValueError("until must be after since")
        return self


class SimulationSuite(Strict):
    kind: Literal["simulation"]
    scene: Literal["aloha_transfer_cube", "aloha_insertion"] = "aloha_transfer_cube"
    episodes: int = Field(default=50, ge=1, le=10_000)
    first_seed: int = Field(default=1000, ge=0, le=2**31 - 1)
    max_steps: int = Field(default=400, ge=1, le=100_000)
    margin: float = Field(default=0.1, ge=0, lt=1)


class ReplaySuite(Strict):
    kind: Literal["replay"]
    filter: EpisodeFilter = Field(default_factory=EpisodeFilter)
    horizon: int | None = Field(default=None, ge=1, le=10_000)
    stride: int = Field(default=5, ge=1, le=10_000)
    max_episodes: int = Field(default=200, ge=1, le=100_000)
    relative: float = Field(default=0.25, ge=0, le=100)
    absolute: float = Field(default=0.01, ge=0, le=1)
    batch_size: int = Field(default=8, ge=1, le=1024)


EvaluationSuite = Annotated[SimulationSuite | ReplaySuite, Field(discriminator="kind")]


class EvaluationSpec(Strict):
    candidate_revision_id: RevisionId
    baseline_revision_id: RevisionId | None = None
    suite: EvaluationSuite


class EvaluationCreate(Strict):
    spec: EvaluationSpec


class TrainingRecipe(Strict):
    steps: int = Field(default=2000, ge=1, le=10_000_000)
    batch_size: int = Field(default=8, ge=1, le=4096)
    learning_rate: float | None = Field(default=None, gt=0, lt=1)
    seed: int = Field(default=1000, ge=0, le=2**31 - 1)
    num_workers: int = Field(default=2, ge=0, le=64)
    normalization: Literal["base", "dataset"] = "base"
    log_every: int = Field(default=50, ge=1, le=1_000_000)


ArtifactVersion = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")]


class TrainingJobSpec(Strict):
    base_revision_id: RevisionId
    episodes: EpisodeFilter = Field(default_factory=EpisodeFilter)
    recipe: TrainingRecipe = Field(default_factory=TrainingRecipe)
    artifact_name: Name | None = None
    artifact_version: ArtifactVersion | None = None


class TrainingJobCreate(Strict):
    spec: TrainingJobSpec


Metric = Literal[
    "deadline_miss_rate",
    "observation_age_p99_ms",
    "starvation_events",
    "robots_per_gpu",
    "gpu_utilization",
    "gpu_hours_per_robot_hour",
]


class MetricsFilters(Strict):
    site_id: SiteId | None = None
    robot_id: RobotId | None = None
    revision_id: RevisionId | None = None


class MetricsQuery(Strict):
    metric: Metric
    start: Any
    end: Any
    step_seconds: int = Field(ge=10, le=86400)
    filters: MetricsFilters = Field(default_factory=MetricsFilters)


class ApiTokenCreate(Strict):
    display_name: DisplayName
    scopes: list[Literal["read", "deploy", "admin"]] = Field(min_length=1)
    project_ids: list[ProjectId] = Field(default_factory=list)
    expires_in_days: int = Field(ge=1, le=365)


class SupportGrantCreate(Strict):
    site_id: SiteId
    operations: list[
        Literal["read_status", "collect_diagnostics", "prepare_revision", "rollback"]
    ] = Field(min_length=1)
    duration_minutes: int = Field(ge=15, le=1440)
    justification: Annotated[str, StringConstraints(min_length=10, max_length=2000)]


MemberRole = Literal["viewer", "deployer", "site_operator", "admin"]
# Deliberately simple: the address only has to match the IdP's verified email claim exactly.
Email = Annotated[
    str,
    StringConstraints(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320, strip_whitespace=True),
]


class MemberCreate(Strict):
    role: MemberRole
    email: Email | None = None
    subject: Annotated[str, StringConstraints(min_length=3, max_length=512)] | None = None

    @model_validator(mode="after")
    def _one_identity(self) -> Self:
        if (self.email is None) == (self.subject is None):
            raise ValueError("give exactly one of email or subject")
        return self


class MemberUpdate(Strict):
    role: MemberRole | None = None


class SpecPatch(Strict):
    """``{"spec": {...}}`` merge-patch envelope; fields are checked after merging."""

    spec: dict[str, Any]


MUTABLE_FIELDS: dict[str, frozenset[str]] = {
    "project": frozenset({"display_name", "labels"}),
    "site": frozenset({"display_name", "labels", "offline_authorization_hours"}),
    "server": frozenset({"display_name", "labels", "allocated_gpu_indexes"}),
    "robot": frozenset({"display_name", "labels", "robot_profile_id"}),
    "deployment": frozenset(
        {"display_name", "labels", "robot_ids", "desired_revision_id", "capture", "release_gates"}
    ),
}
