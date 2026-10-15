"""Generated from api/openapi.yaml by tools/gen_sdk.py. Do not edit."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class ActivationRequest(TypedDict):
    """
    A request to activate a prepared revision. Motion requires local approval at the site at
    a task boundary; the cloud cannot authorize it (design §20.9).
    """

    metadata: Metadata
    revision_id: RevisionId
    robot_ids: list[RobotId]
    operation_id: OperationId
    state: Literal[
        "AwaitingLocalApproval", "Approved", "Refused", "Activated", "Failed", "Cancelled"
    ]
    approved_by: NotRequired[str | None]
    note: NotRequired[str]


class ActivationRequestCreate(TypedDict):
    revision_id: RevisionId
    robot_ids: NotRequired[list[RobotId]]
    note: NotRequired[str]


type ActivationRequestId = str


class ActivationRequestPage(TypedDict):
    items: list[ActivationRequest]
    page: PageInfo


class ApiToken(TypedDict):
    metadata: Metadata
    display_name: DisplayName
    scopes: list[ApiTokenScope]
    project_ids: NotRequired[list[ProjectId]]
    expires_at: Timestamp
    last_used_at: NotRequired[Timestamp | None]


class ApiTokenCreate(TypedDict):
    display_name: DisplayName
    scopes: list[ApiTokenScope]
    project_ids: NotRequired[list[ProjectId]]
    expires_in_days: int


type ApiTokenId = str


class ApiTokenPage(TypedDict):
    items: list[ApiToken]
    page: PageInfo


type ApiTokenScope = Literal["read", "deploy", "admin"]


class ApiTokenWithSecret(TypedDict):
    metadata: Metadata
    display_name: DisplayName
    scopes: list[ApiTokenScope]
    project_ids: NotRequired[list[ProjectId]]
    expires_at: Timestamp
    last_used_at: NotRequired[Timestamp | None]
    secret: str


class ArtifactSignature(TypedDict):
    """
    A publisher's Ed25519 signature over a model artifact's digest (design §17.3), made with
    `neura-contracts sign`. Sites run an artifact only if a publisher key their operator
    trusts signed it.
    """

    key_id: str
    public_key: str
    signature: str


class AuditRecord(TypedDict):
    metadata: Metadata
    actor: str
    actor_kind: NotRequired[Literal["user", "api_token", "site_agent", "support", "system"]]
    action: str
    target: str
    result: Literal["succeeded", "failed", "denied"]
    reason: NotRequired[ReasonCode | None]
    request_id: NotRequired[str]
    occurred_at: Timestamp


type AuditRecordId = str


class AuditRecordPage(TypedDict):
    items: list[AuditRecord]
    page: PageInfo


class CapacityEstimateAssumptions(TypedDict):
    """
    The inputs of the estimate, so it can be checked and reproduced. Absent when the site
    runs minimal telemetry (design §18.2): its inputs, including measured latency, stay at
    the site.
    """

    control_period_ms: NotRequired[float]
    starvation_budget_ms: NotRequired[float]
    request_interval_ms: NotRequired[float]
    link_allowance_ms: NotRequired[float]
    dispatch_freshness_ms: NotRequired[float | None]
    acceptance_freshness_ms: NotRequired[float | None]
    max_batch: NotRequired[int]
    batch_window_ms: NotRequired[float]
    reserve_fraction: NotRequired[float]
    latency_p99_ms_by_batch: NotRequired[dict[str, float]]
    memory_available_mib: NotRequired[int | None]
    device: NotRequired[str | None]
    co_located_groups: NotRequired[int]


class CapacityEstimate(TypedDict):
    """
    How many robots one batch group on this site's server can serve at this revision's
    execution profile, from the same admission model the gateway enforces (design §9.3).
    `source` says whether the latency profile was measured on the site's device at prepare or
    configured; `sharing_case` says how this revision's group shares its device (design
    §9.5).
    """

    max_robots: int
    limiting_factor: Literal[
        "deadline",
        "dispatch_freshness",
        "acceptance_freshness",
        "memory",
        "throughput",
        "search_limit",
    ]
    source: Literal["configured_profile", "measured_profile"]
    sharing_case: NotRequired[
        Literal["same_weights", "multi_adapter", "co_located_fine_tunes", "different_architectures"]
    ]
    assumptions: NotRequired[CapacityEstimateAssumptions]
    estimated_at: Timestamp


class CompatibilityCheckRequest(TypedDict):
    model_artifact_id: ModelArtifactId
    robot_profile_id: RobotProfileId
    execution_profile_id: ExecutionProfileId


class CompatibilityIssue(TypedDict):
    code: ReasonCode
    path: str
    message: str


class CompatibilityReport(TypedDict):
    compatible: bool
    artifact_digest: Digest
    robot_profile_digest: Digest
    execution_profile_digest: Digest
    issues: list[CompatibilityIssue]


type ConsolePhase = Literal[
    "Unknown", "Preparing", "Ready", "Running", "Waiting", "NeedsAttention", "Offline"
]


class ContractDocument(TypedDict):
    """
    A contract document (ModelArtifact, RobotProfile or ExecutionProfile) as defined by the
    JSON Schemas in schemas/. Validated server-side by neura-contracts.
    """

    api_version: Literal["neura.dev/v1alpha1"]
    kind: Literal["ModelArtifact", "RobotProfile", "ExecutionProfile"]
    metadata: dict[str, Any]
    spec: dict[str, Any]


class CurrentPrincipalOrganizationsItem(TypedDict):
    id: OrganizationId
    display_name: str
    permissions: list[Literal["read", "deploy", "admin"]]


class CurrentPrincipal(TypedDict):
    """The authenticated caller and the organizations it can access."""

    subject: str
    kind: Literal["user", "api_token", "site_agent", "support", "system"]
    organizations: list[CurrentPrincipalOrganizationsItem]
    project_ids: NotRequired[list[ProjectId] | None]


class DeploymentStatus(TypedDict):
    """
    Desired, prepared and active revisions are reported separately; a successful request
    never implies a robot runs the revision (design §20.2).
    """

    phase: DeploymentPhase
    reason: NotRequired[ReasonCode | None]
    message: NotRequired[str]
    prepared_revision_id: NotRequired[RevisionId | None]
    active_revision_ids: NotRequired[dict[str, RevisionId | None]]
    observed_at: NotRequired[Timestamp | None]
    data_age_seconds: NotRequired[float | None]


class Deployment(TypedDict):
    metadata: Metadata
    spec: DeploymentSpec
    status: DeploymentStatus


class DeploymentCreate(TypedDict):
    spec: DeploymentSpec


type DeploymentId = str


class DeploymentPage(TypedDict):
    items: list[Deployment]
    page: PageInfo


type DeploymentPhase = Literal[
    "Pending", "Preparing", "Prepared", "Activating", "Active", "RollingBack", "NeedsAttention"
]


class DeploymentSpecRollout(TypedDict):
    strategy: Literal["all_at_task_boundary", "single_robot_canary"]
    canary_robot_id: NotRequired[RobotId]


class DeploymentSpec(TypedDict):
    name: Name
    display_name: DisplayName
    site_id: SiteId
    robot_ids: list[RobotId]
    desired_revision_id: NotRequired[RevisionId | None]
    rollout: DeploymentSpecRollout
    labels: NotRequired[Labels]


class DeploymentUpdateSpec(TypedDict):
    display_name: NotRequired[DisplayName]
    labels: NotRequired[Labels]
    robot_ids: NotRequired[list[RobotId]]
    desired_revision_id: NotRequired[RevisionId | None]


class DeploymentUpdate(TypedDict):
    """JSON Merge Patch (RFC 7396) of the mutable spec fields."""

    spec: NotRequired[DeploymentUpdateSpec]


type Digest = str


type DisplayName = str


class EnrollmentToken(TypedDict):
    """
    One-time token for enrolling a server (design §17.2). The secret is shown only in this
    response.
    """

    id: EnrollmentTokenId
    site_id: SiteId
    secret: str
    expires_at: Timestamp
    install_command: str


class EnrollmentTokenCreate(TypedDict):
    ttl_minutes: NotRequired[int]


type EnrollmentTokenId = str


class Event(TypedDict):
    """
    A change notification. Events say what changed; re-read the resource for its current
    state (design §20.6).
    """

    cursor: str
    type: Literal[
        "resource.created",
        "resource.updated",
        "resource.deleted",
        "operation.updated",
        "fault.raised",
        "fault.cleared",
    ]
    resource: str
    resource_version: NotRequired[str]
    occurred_at: Timestamp


class ExecutionProfile(TypedDict):
    """An immutable, registered ExecutionProfile document. Changes are new documents."""

    metadata: Metadata
    digest: Digest
    document: ContractDocument


class ExecutionProfileCreate(TypedDict):
    document: ContractDocument


type ExecutionProfileId = str


class ExecutionProfilePage(TypedDict):
    items: list[ExecutionProfile]
    page: PageInfo


class Fault(TypedDict):
    metadata: Metadata
    robot_id: RobotId
    session_id: NotRequired[SessionId | None]
    revision_id: NotRequired[RevisionId | None]
    reason: ReasonCode
    message: NotRequired[str]
    latched: bool
    local_response: NotRequired[str | None]
    occurred_at: Timestamp
    cleared_at: NotRequired[Timestamp | None]
    trace_ids: NotRequired[list[str]]


type FaultId = str


class FaultPage(TypedDict):
    items: list[Fault]
    page: PageInfo


class GpuDevice(TypedDict):
    index: int
    uuid: NotRequired[str]
    model: str
    memory_mib: int
    mig_enabled: NotRequired[bool]
    allocated_to_neura: bool


type Labels = dict[str, str]


class Member(TypedDict):
    """
    A person's membership in an organization. Invited by verified email or added by SSO
    subject; an invitation becomes active when the person first signs in with that verified
    email.
    """

    metadata: Metadata
    role: MemberRole
    email: NotRequired[str | None]
    subject: NotRequired[str | None]
    state: Literal["Invited", "Active"]


class MemberCreate(TypedDict):
    """Exactly one of `email` (invite) or `subject` (an SSO subject `<issuer>|<sub>`)."""

    role: MemberRole
    email: NotRequired[str]
    subject: NotRequired[str]


type MemberId = str


class MemberPage(TypedDict):
    items: list[Member]
    page: PageInfo


type MemberRole = Literal["viewer", "deployer", "site_operator", "admin"]


class MemberUpdate(TypedDict):
    """JSON Merge Patch (RFC 7396)."""

    role: NotRequired[MemberRole]


class Metadata(TypedDict):
    """Identity and bookkeeping common to every resource (design §20.2)."""

    id: str
    project_id: NotRequired[ProjectId]
    organization_id: NotRequired[OrganizationId]
    version: str
    created_at: Timestamp
    updated_at: Timestamp
    created_by: NotRequired[str]


class MetricsQueryFilters(TypedDict):
    site_id: NotRequired[SiteId]
    robot_id: NotRequired[RobotId]
    revision_id: NotRequired[RevisionId]


class MetricsQuery(TypedDict):
    metric: Literal[
        "deadline_miss_rate",
        "observation_age_p99_ms",
        "starvation_events",
        "robots_per_gpu",
        "gpu_utilization",
        "gpu_hours_per_robot_hour",
    ]
    start: Timestamp
    end: Timestamp
    step_seconds: int
    filters: NotRequired[MetricsQueryFilters]


class MetricsResultSeriesItem(TypedDict):
    labels: dict[str, str]
    points: list[list[Timestamp | float | None]]


class MetricsResult(TypedDict):
    """Aggregates only; never payloads (design §18)."""

    metric: str
    series: list[MetricsResultSeriesItem]


class ModelArtifact(TypedDict):
    """An immutable, registered ModelArtifact document. Changes are new documents."""

    metadata: Metadata
    digest: Digest
    document: ContractDocument
    signatures: list[ArtifactSignature]


class ModelArtifactCreate(TypedDict):
    document: ContractDocument
    signatures: NotRequired[list[ArtifactSignature]]


type ModelArtifactId = str


class ModelArtifactPage(TypedDict):
    items: list[ModelArtifact]
    page: PageInfo


type Name = str


class ObservedStatus(TypedDict):
    """Observed state, written only from site observations (design §20.2). Never set by clients."""

    phase: ConsolePhase
    reason: NotRequired[ReasonCode | None]
    message: NotRequired[str]
    observed_revision_id: NotRequired[RevisionId | None]
    observed_at: NotRequired[Timestamp | None]
    data_age_seconds: NotRequired[float | None]


class Operation(TypedDict):
    """Long-running work that completes only when the site acknowledges it (design §20.4)."""

    metadata: Metadata
    kind: Literal["prepare_revision", "activate_revision", "rollback", "collect_diagnostics"]
    target: str
    state: OperationState
    reason: NotRequired[ReasonCode | None]
    message: NotRequired[str]
    started_at: NotRequired[Timestamp | None]
    finished_at: NotRequired[Timestamp | None]
    result: NotRequired[dict[str, Any]]


type OperationId = str


type OperationState = Literal["Pending", "Running", "Succeeded", "Failed", "Cancelled"]


class OrganizationSpec(TypedDict):
    display_name: DisplayName


class Organization(TypedDict):
    metadata: Metadata
    spec: OrganizationSpec


type OrganizationId = str


class PageInfo(TypedDict):
    next_cursor: str | None


class PreflightCheck(TypedDict):
    name: str
    passed: bool
    reason: NotRequired[ReasonCode | None]
    detail: NotRequired[str]
    fix: NotRequired[str]


class Problem(TypedDict):
    """RFC 9457 problem details with a Neura reason code."""

    type: str
    title: str
    status: int
    detail: NotRequired[str]
    instance: NotRequired[str]
    reason: ReasonCode
    retryable: bool
    errors: NotRequired[list[str]]


class Project(TypedDict):
    metadata: Metadata
    spec: ProjectSpec


class ProjectCreate(TypedDict):
    spec: ProjectSpec


type ProjectId = str


class ProjectPage(TypedDict):
    items: list[Project]
    page: PageInfo


class ProjectSpec(TypedDict):
    name: Name
    display_name: DisplayName
    labels: NotRequired[Labels]


class ProjectUpdateSpec(TypedDict):
    display_name: NotRequired[DisplayName]
    labels: NotRequired[Labels]


class ProjectUpdate(TypedDict):
    """JSON Merge Patch (RFC 7396) of the mutable spec fields."""

    spec: NotRequired[ProjectUpdateSpec]


type ReasonCode = str


class RevisionStatus(TypedDict):
    phase: RevisionPhase
    revision_digest: Digest
    compatibility: NotRequired[CompatibilityReport]
    reason: NotRequired[ReasonCode | None]
    message: NotRequired[str]
    capacity: NotRequired[CapacityEstimate | None]


class Revision(TypedDict):
    """An immutable deployment revision: one artifact, robot profile and execution profile."""

    metadata: Metadata
    spec: RevisionSpec
    status: RevisionStatus


class RevisionCreate(TypedDict):
    spec: RevisionSpec


type RevisionId = str


class RevisionPage(TypedDict):
    items: list[Revision]
    page: PageInfo


type RevisionPhase = Literal["Created", "Preparing", "Prepared", "Failed", "Retired"]


class RevisionSpec(TypedDict):
    model_artifact_id: ModelArtifactId
    execution_profile_id: ExecutionProfileId
    robot_profile_id: RobotProfileId
    notes: NotRequired[str]


class RobotStatus(TypedDict):
    phase: ConsolePhase
    reason: NotRequired[ReasonCode | None]
    message: NotRequired[str]
    observed_revision_id: NotRequired[RevisionId | None]
    observed_at: NotRequired[Timestamp | None]
    data_age_seconds: NotRequired[float | None]
    active_revision_id: NotRequired[RevisionId | None]
    lease_holder_session_id: NotRequired[SessionId | None]
    task_epoch: NotRequired[int | None]


class Robot(TypedDict):
    metadata: Metadata
    spec: RobotSpec
    status: RobotStatus


class RobotCreate(TypedDict):
    spec: RobotSpec


type RobotId = str


class RobotPage(TypedDict):
    items: list[Robot]
    page: PageInfo


class RobotProfile(TypedDict):
    """An immutable, registered RobotProfile document. Changes are new documents."""

    metadata: Metadata
    digest: Digest
    document: ContractDocument


class RobotProfileCreate(TypedDict):
    document: ContractDocument


type RobotProfileId = str


class RobotProfilePage(TypedDict):
    items: list[RobotProfile]
    page: PageInfo


class RobotSpec(TypedDict):
    name: Name
    display_name: DisplayName
    site_id: SiteId
    robot_profile_id: RobotProfileId
    labels: NotRequired[Labels]


class RobotUpdateSpec(TypedDict):
    display_name: NotRequired[DisplayName]
    labels: NotRequired[Labels]
    robot_profile_id: NotRequired[RobotProfileId]


class RobotUpdate(TypedDict):
    """JSON Merge Patch (RFC 7396) of the mutable spec fields."""

    spec: NotRequired[RobotUpdateSpec]


class RollbackCreate(TypedDict):
    to_revision_id: RevisionId
    robot_ids: NotRequired[list[RobotId]]
    note: NotRequired[str]


type RollbackId = str


class SealedWeights(TypedDict):
    """
    A weight file in Neura's registry, sealed with the customer's key (design §18.2). The
    control plane stores the ciphertext only and cannot open it; sites fetch it by digest and
    open it with the key configured on the server.
    """

    project_id: ProjectId
    digest: Digest
    key_id: str
    plaintext_bytes: int
    sealed_bytes: int
    sealed_sha256: str
    created_at: str
    created_by: str
    version: str


class SealedWeightsPage(TypedDict):
    items: list[SealedWeights]
    page: PageInfo


class ServerStatus(TypedDict):
    phase: ConsolePhase
    reason: NotRequired[ReasonCode | None]
    message: NotRequired[str]
    observed_revision_id: NotRequired[RevisionId | None]
    observed_at: NotRequired[Timestamp | None]
    data_age_seconds: NotRequired[float | None]
    site_id: NotRequired[SiteId]
    hostname: NotRequired[str]
    driver_version: NotRequired[str | None]
    cuda_version: NotRequired[str | None]
    gpus: NotRequired[list[GpuDevice]]
    preflight: NotRequired[list[PreflightCheck]]


class Server(TypedDict):
    metadata: Metadata
    spec: ServerSpec
    status: ServerStatus


class ServerCreate(TypedDict):
    spec: ServerSpec


type ServerId = str


class ServerPage(TypedDict):
    items: list[Server]
    page: PageInfo


class ServerSpec(TypedDict):
    display_name: DisplayName
    labels: NotRequired[Labels]
    allocated_gpu_indexes: list[int]


class ServerUpdateSpec(TypedDict):
    display_name: NotRequired[DisplayName]
    labels: NotRequired[Labels]
    allocated_gpu_indexes: NotRequired[list[int]]


class ServerUpdate(TypedDict):
    """JSON Merge Patch (RFC 7396) of the mutable spec fields."""

    spec: NotRequired[ServerUpdateSpec]


class ServiceMetaAuthenticationOidcOption0(TypedDict):
    """SSO with OIDC authorization code + PKCE; null when not configured."""

    issuer: str
    client_id: str
    audience: NotRequired[str | None]
    scopes: list[str]


class ServiceMetaAuthentication(TypedDict):
    """How people and machines sign in, for the console and other clients."""

    api_tokens: bool
    oidc: ServiceMetaAuthenticationOidcOption0 | None


class ServiceMeta(TypedDict):
    service: str
    service_version: str
    api_version: Literal["v1"]
    api_spec_version: str
    contracts_api_version: str
    protocol_version: str
    environment: NotRequired[str]
    authentication: ServiceMetaAuthentication


class SessionMetrics(TypedDict):
    buffer_actions: NotRequired[int]
    observation_age_p99_ms: NotRequired[float]
    deadline_misses_last_hour: NotRequired[int]
    rtt_p99_ms: NotRequired[float]
    chunks: NotRequired[int]
    dispatched_actions: NotRequired[int]


class Session(TypedDict):
    metadata: Metadata
    robot_id: RobotId
    site_id: SiteId
    revision_id: RevisionId
    lease_generation: int
    task_epoch: int
    state: Literal["Disconnected", "Ready", "Armed", "Running", "Waiting", "Faulted"]
    reason: NotRequired[ReasonCode | None]
    metrics: NotRequired[SessionMetrics]
    data_age_seconds: NotRequired[float | None]


type SessionId = str


class SessionPage(TypedDict):
    items: list[Session]
    page: PageInfo


class SiteStatus(TypedDict):
    phase: ConsolePhase
    reason: NotRequired[ReasonCode | None]
    message: NotRequired[str]
    observed_revision_id: NotRequired[RevisionId | None]
    observed_at: NotRequired[Timestamp | None]
    data_age_seconds: NotRequired[float | None]
    agent_version: NotRequired[str | None]
    connected: NotRequired[bool]
    offline_authorization_expires_at: NotRequired[Timestamp | None]


class Site(TypedDict):
    metadata: Metadata
    spec: SiteSpec
    status: SiteStatus


class SiteCreate(TypedDict):
    spec: SiteSpec


type SiteId = str


class SitePage(TypedDict):
    items: list[Site]
    page: PageInfo


class SiteSpec(TypedDict):
    name: Name
    display_name: DisplayName
    labels: NotRequired[Labels]
    offline_authorization_hours: int


class SiteUpdateSpec(TypedDict):
    display_name: NotRequired[DisplayName]
    labels: NotRequired[Labels]
    offline_authorization_hours: NotRequired[int]


class SiteUpdate(TypedDict):
    """JSON Merge Patch (RFC 7396) of the mutable spec fields."""

    spec: NotRequired[SiteUpdateSpec]


class SupportGrant(TypedDict):
    """Customer-approved, time-boxed break-glass access (design §18.2)."""

    metadata: Metadata
    site_id: SiteId
    operations: list[str]
    justification: NotRequired[str]
    expires_at: Timestamp
    state: Literal["Active", "Expired", "Revoked"]


class SupportGrantCreate(TypedDict):
    site_id: SiteId
    operations: list[Literal["read_status", "collect_diagnostics", "prepare_revision", "rollback"]]
    duration_minutes: int
    justification: str


type SupportGrantId = str


class SupportGrantPage(TypedDict):
    items: list[SupportGrant]
    page: PageInfo


type Timestamp = str
