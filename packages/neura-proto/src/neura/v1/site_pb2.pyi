# GENERATED FILE: do not edit. Source: proto/neura/v1/site.proto. Regenerate with `make gen`.
from neura.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FetchSealedWeightsRequest(_message.Message):
    __slots__ = ("digest",)
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    digest: str
    def __init__(self, digest: _Optional[str] = ...) -> None: ...

class SealedWeightsChunk(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...

class EnrollRequest(_message.Message):
    __slots__ = ("protocol_version", "enrollment_token", "public_key", "agent_version", "facts", "tls_csr")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    ENROLLMENT_TOKEN_FIELD_NUMBER: _ClassVar[int]
    PUBLIC_KEY_FIELD_NUMBER: _ClassVar[int]
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    FACTS_FIELD_NUMBER: _ClassVar[int]
    TLS_CSR_FIELD_NUMBER: _ClassVar[int]
    protocol_version: _common_pb2.ProtocolVersion
    enrollment_token: str
    public_key: bytes
    agent_version: str
    facts: ServerFacts
    tls_csr: bytes
    def __init__(self, protocol_version: _Optional[_Union[_common_pb2.ProtocolVersion, str]] = ..., enrollment_token: _Optional[str] = ..., public_key: _Optional[bytes] = ..., agent_version: _Optional[str] = ..., facts: _Optional[_Union[ServerFacts, _Mapping]] = ..., tls_csr: _Optional[bytes] = ...) -> None: ...

class EnrollResponse(_message.Message):
    __slots__ = ("enrolled", "rejected")
    ENROLLED_FIELD_NUMBER: _ClassVar[int]
    REJECTED_FIELD_NUMBER: _ClassVar[int]
    enrolled: Enrolled
    rejected: _common_pb2.Reason
    def __init__(self, enrolled: _Optional[_Union[Enrolled, _Mapping]] = ..., rejected: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ...) -> None: ...

class Enrolled(_message.Message):
    __slots__ = ("server_id", "site_id", "project_id", "manifest_public_key", "manifest_key_id", "client_certificate", "client_ca", "agent_endpoint")
    SERVER_ID_FIELD_NUMBER: _ClassVar[int]
    SITE_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    MANIFEST_PUBLIC_KEY_FIELD_NUMBER: _ClassVar[int]
    MANIFEST_KEY_ID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_CERTIFICATE_FIELD_NUMBER: _ClassVar[int]
    CLIENT_CA_FIELD_NUMBER: _ClassVar[int]
    AGENT_ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    server_id: str
    site_id: str
    project_id: str
    manifest_public_key: bytes
    manifest_key_id: str
    client_certificate: bytes
    client_ca: bytes
    agent_endpoint: str
    def __init__(self, server_id: _Optional[str] = ..., site_id: _Optional[str] = ..., project_id: _Optional[str] = ..., manifest_public_key: _Optional[bytes] = ..., manifest_key_id: _Optional[str] = ..., client_certificate: _Optional[bytes] = ..., client_ca: _Optional[bytes] = ..., agent_endpoint: _Optional[str] = ...) -> None: ...

class ServerFacts(_message.Message):
    __slots__ = ("hostname", "driver_version", "cuda_version", "gpus", "preflight")
    HOSTNAME_FIELD_NUMBER: _ClassVar[int]
    DRIVER_VERSION_FIELD_NUMBER: _ClassVar[int]
    CUDA_VERSION_FIELD_NUMBER: _ClassVar[int]
    GPUS_FIELD_NUMBER: _ClassVar[int]
    PREFLIGHT_FIELD_NUMBER: _ClassVar[int]
    hostname: str
    driver_version: str
    cuda_version: str
    gpus: _containers.RepeatedCompositeFieldContainer[GpuInfo]
    preflight: _containers.RepeatedCompositeFieldContainer[PreflightResult]
    def __init__(self, hostname: _Optional[str] = ..., driver_version: _Optional[str] = ..., cuda_version: _Optional[str] = ..., gpus: _Optional[_Iterable[_Union[GpuInfo, _Mapping]]] = ..., preflight: _Optional[_Iterable[_Union[PreflightResult, _Mapping]]] = ...) -> None: ...

class GpuInfo(_message.Message):
    __slots__ = ("index", "uuid", "model", "memory_mib", "mig_enabled")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    UUID_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    MEMORY_MIB_FIELD_NUMBER: _ClassVar[int]
    MIG_ENABLED_FIELD_NUMBER: _ClassVar[int]
    index: int
    uuid: str
    model: str
    memory_mib: int
    mig_enabled: bool
    def __init__(self, index: _Optional[int] = ..., uuid: _Optional[str] = ..., model: _Optional[str] = ..., memory_mib: _Optional[int] = ..., mig_enabled: _Optional[bool] = ...) -> None: ...

class PreflightResult(_message.Message):
    __slots__ = ("name", "passed", "reason", "detail", "fix")
    NAME_FIELD_NUMBER: _ClassVar[int]
    PASSED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    FIX_FIELD_NUMBER: _ClassVar[int]
    name: str
    passed: bool
    reason: str
    detail: str
    fix: str
    def __init__(self, name: _Optional[str] = ..., passed: _Optional[bool] = ..., reason: _Optional[str] = ..., detail: _Optional[str] = ..., fix: _Optional[str] = ...) -> None: ...

class AgentMessage(_message.Message):
    __slots__ = ("hello", "ack", "status", "telemetry", "operation", "facts", "certificate_request")
    HELLO_FIELD_NUMBER: _ClassVar[int]
    ACK_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TELEMETRY_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    FACTS_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_REQUEST_FIELD_NUMBER: _ClassVar[int]
    hello: AgentHello
    ack: Acknowledgement
    status: SiteStatus
    telemetry: TelemetryBatch
    operation: OperationReport
    facts: ServerFacts
    certificate_request: CertificateRequest
    def __init__(self, hello: _Optional[_Union[AgentHello, _Mapping]] = ..., ack: _Optional[_Union[Acknowledgement, _Mapping]] = ..., status: _Optional[_Union[SiteStatus, _Mapping]] = ..., telemetry: _Optional[_Union[TelemetryBatch, _Mapping]] = ..., operation: _Optional[_Union[OperationReport, _Mapping]] = ..., facts: _Optional[_Union[ServerFacts, _Mapping]] = ..., certificate_request: _Optional[_Union[CertificateRequest, _Mapping]] = ...) -> None: ...

class CertificateRequest(_message.Message):
    __slots__ = ("csr",)
    CSR_FIELD_NUMBER: _ClassVar[int]
    csr: bytes
    def __init__(self, csr: _Optional[bytes] = ...) -> None: ...

class CertificateIssued(_message.Message):
    __slots__ = ("certificate", "ca")
    CERTIFICATE_FIELD_NUMBER: _ClassVar[int]
    CA_FIELD_NUMBER: _ClassVar[int]
    certificate: bytes
    ca: bytes
    def __init__(self, certificate: _Optional[bytes] = ..., ca: _Optional[bytes] = ...) -> None: ...

class OperationReport(_message.Message):
    __slots__ = ("command_id", "operation_id", "state", "reason", "message", "result_json")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RESULT_JSON_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    operation_id: str
    state: str
    reason: _common_pb2.Reason
    message: str
    result_json: bytes
    def __init__(self, command_id: _Optional[str] = ..., operation_id: _Optional[str] = ..., state: _Optional[str] = ..., reason: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ..., message: _Optional[str] = ..., result_json: _Optional[bytes] = ...) -> None: ...

class ControlPlaneMessage(_message.Message):
    __slots__ = ("desired_state", "command", "contact", "certificate")
    DESIRED_STATE_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    CONTACT_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_FIELD_NUMBER: _ClassVar[int]
    desired_state: DesiredState
    command: Command
    contact: ContactProof
    certificate: CertificateIssued
    def __init__(self, desired_state: _Optional[_Union[DesiredState, _Mapping]] = ..., command: _Optional[_Union[Command, _Mapping]] = ..., contact: _Optional[_Union[ContactProof, _Mapping]] = ..., certificate: _Optional[_Union[CertificateIssued, _Mapping]] = ...) -> None: ...

class ContactProof(_message.Message):
    __slots__ = ("nonce", "signature")
    NONCE_FIELD_NUMBER: _ClassVar[int]
    SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    nonce: bytes
    signature: Signature
    def __init__(self, nonce: _Optional[bytes] = ..., signature: _Optional[_Union[Signature, _Mapping]] = ...) -> None: ...

class AgentHello(_message.Message):
    __slots__ = ("protocol_version", "site_id", "agent_version", "committed_generation")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    SITE_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    COMMITTED_GENERATION_FIELD_NUMBER: _ClassVar[int]
    protocol_version: _common_pb2.ProtocolVersion
    site_id: str
    agent_version: str
    committed_generation: int
    def __init__(self, protocol_version: _Optional[_Union[_common_pb2.ProtocolVersion, str]] = ..., site_id: _Optional[str] = ..., agent_version: _Optional[str] = ..., committed_generation: _Optional[int] = ...) -> None: ...

class DesiredState(_message.Message):
    __slots__ = ("generation", "manifest", "signature")
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    MANIFEST_FIELD_NUMBER: _ClassVar[int]
    SIGNATURE_FIELD_NUMBER: _ClassVar[int]
    generation: int
    manifest: bytes
    signature: Signature
    def __init__(self, generation: _Optional[int] = ..., manifest: _Optional[bytes] = ..., signature: _Optional[_Union[Signature, _Mapping]] = ...) -> None: ...

class Signature(_message.Message):
    __slots__ = ("key_id", "value")
    KEY_ID_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    key_id: str
    value: bytes
    def __init__(self, key_id: _Optional[str] = ..., value: _Optional[bytes] = ...) -> None: ...

class Command(_message.Message):
    __slots__ = ("command_id", "prepare", "request_activation", "rollback", "collect_diagnostics", "cancel", "upgrade_agent", "operation_id")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    PREPARE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ACTIVATION_FIELD_NUMBER: _ClassVar[int]
    ROLLBACK_FIELD_NUMBER: _ClassVar[int]
    COLLECT_DIAGNOSTICS_FIELD_NUMBER: _ClassVar[int]
    CANCEL_FIELD_NUMBER: _ClassVar[int]
    UPGRADE_AGENT_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    prepare: PrepareRevision
    request_activation: RequestActivation
    rollback: Rollback
    collect_diagnostics: CollectDiagnostics
    cancel: CancelOperation
    upgrade_agent: UpgradeAgent
    operation_id: str
    def __init__(self, command_id: _Optional[str] = ..., prepare: _Optional[_Union[PrepareRevision, _Mapping]] = ..., request_activation: _Optional[_Union[RequestActivation, _Mapping]] = ..., rollback: _Optional[_Union[Rollback, _Mapping]] = ..., collect_diagnostics: _Optional[_Union[CollectDiagnostics, _Mapping]] = ..., cancel: _Optional[_Union[CancelOperation, _Mapping]] = ..., upgrade_agent: _Optional[_Union[UpgradeAgent, _Mapping]] = ..., operation_id: _Optional[str] = ...) -> None: ...

class UpgradeAgent(_message.Message):
    __slots__ = ("server_id", "version", "bundle_url", "bundle_sha256")
    SERVER_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    BUNDLE_URL_FIELD_NUMBER: _ClassVar[int]
    BUNDLE_SHA256_FIELD_NUMBER: _ClassVar[int]
    server_id: str
    version: str
    bundle_url: str
    bundle_sha256: str
    def __init__(self, server_id: _Optional[str] = ..., version: _Optional[str] = ..., bundle_url: _Optional[str] = ..., bundle_sha256: _Optional[str] = ...) -> None: ...

class CancelOperation(_message.Message):
    __slots__ = ("operation_id",)
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    operation_id: str
    def __init__(self, operation_id: _Optional[str] = ...) -> None: ...

class PrepareRevision(_message.Message):
    __slots__ = ("revision_id",)
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    revision_id: str
    def __init__(self, revision_id: _Optional[str] = ...) -> None: ...

class RequestActivation(_message.Message):
    __slots__ = ("revision_id", "robot_ids")
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    ROBOT_IDS_FIELD_NUMBER: _ClassVar[int]
    revision_id: str
    robot_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, revision_id: _Optional[str] = ..., robot_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class Rollback(_message.Message):
    __slots__ = ("to_revision_id", "robot_ids")
    TO_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    ROBOT_IDS_FIELD_NUMBER: _ClassVar[int]
    to_revision_id: str
    robot_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, to_revision_id: _Optional[str] = ..., robot_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class CollectDiagnostics(_message.Message):
    __slots__ = ("scope",)
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    scope: str
    def __init__(self, scope: _Optional[str] = ...) -> None: ...

class Acknowledgement(_message.Message):
    __slots__ = ("desired_state_generation", "command_id", "accepted", "reason")
    DESIRED_STATE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    desired_state_generation: int
    command_id: str
    accepted: bool
    reason: _common_pb2.Reason
    def __init__(self, desired_state_generation: _Optional[int] = ..., command_id: _Optional[str] = ..., accepted: _Optional[bool] = ..., reason: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ...) -> None: ...

class SiteStatus(_message.Message):
    __slots__ = ("agent_time_us", "components", "sessions", "faults", "offline_authorization_remaining_s", "contact_nonce", "capture")
    AGENT_TIME_US_FIELD_NUMBER: _ClassVar[int]
    COMPONENTS_FIELD_NUMBER: _ClassVar[int]
    SESSIONS_FIELD_NUMBER: _ClassVar[int]
    FAULTS_FIELD_NUMBER: _ClassVar[int]
    OFFLINE_AUTHORIZATION_REMAINING_S_FIELD_NUMBER: _ClassVar[int]
    CONTACT_NONCE_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_FIELD_NUMBER: _ClassVar[int]
    agent_time_us: int
    components: _containers.RepeatedCompositeFieldContainer[ComponentStatus]
    sessions: _containers.RepeatedCompositeFieldContainer[RobotSessionStatus]
    faults: _containers.RepeatedCompositeFieldContainer[FaultReport]
    offline_authorization_remaining_s: int
    contact_nonce: bytes
    capture: CaptureSummary
    def __init__(self, agent_time_us: _Optional[int] = ..., components: _Optional[_Iterable[_Union[ComponentStatus, _Mapping]]] = ..., sessions: _Optional[_Iterable[_Union[RobotSessionStatus, _Mapping]]] = ..., faults: _Optional[_Iterable[_Union[FaultReport, _Mapping]]] = ..., offline_authorization_remaining_s: _Optional[int] = ..., contact_nonce: _Optional[bytes] = ..., capture: _Optional[_Union[CaptureSummary, _Mapping]] = ...) -> None: ...

class CaptureSummary(_message.Message):
    __slots__ = ("enabled", "episodes", "bytes", "steps", "outcomes", "revisions", "datasets")
    class OutcomesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    EPISODES_FIELD_NUMBER: _ClassVar[int]
    BYTES_FIELD_NUMBER: _ClassVar[int]
    STEPS_FIELD_NUMBER: _ClassVar[int]
    OUTCOMES_FIELD_NUMBER: _ClassVar[int]
    REVISIONS_FIELD_NUMBER: _ClassVar[int]
    DATASETS_FIELD_NUMBER: _ClassVar[int]
    enabled: bool
    episodes: int
    bytes: int
    steps: int
    outcomes: _containers.ScalarMap[str, int]
    revisions: _containers.RepeatedCompositeFieldContainer[RevisionCapture]
    datasets: int
    def __init__(self, enabled: _Optional[bool] = ..., episodes: _Optional[int] = ..., bytes: _Optional[int] = ..., steps: _Optional[int] = ..., outcomes: _Optional[_Mapping[str, int]] = ..., revisions: _Optional[_Iterable[_Union[RevisionCapture, _Mapping]]] = ..., datasets: _Optional[int] = ...) -> None: ...

class RevisionCapture(_message.Message):
    __slots__ = ("revision_id", "outcomes")
    class OutcomesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    OUTCOMES_FIELD_NUMBER: _ClassVar[int]
    revision_id: str
    outcomes: _containers.ScalarMap[str, int]
    def __init__(self, revision_id: _Optional[str] = ..., outcomes: _Optional[_Mapping[str, int]] = ...) -> None: ...

class RobotSessionStatus(_message.Message):
    __slots__ = ("session_id", "robot_name", "revision_id", "lease_generation", "task_epoch", "state", "reason", "metrics")
    class MetricsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: float
        def __init__(self, key: _Optional[str] = ..., value: _Optional[float] = ...) -> None: ...
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ROBOT_NAME_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    LEASE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    TASK_EPOCH_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    METRICS_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    robot_name: str
    revision_id: str
    lease_generation: int
    task_epoch: int
    state: str
    reason: _common_pb2.Reason
    metrics: _containers.ScalarMap[str, float]
    def __init__(self, session_id: _Optional[str] = ..., robot_name: _Optional[str] = ..., revision_id: _Optional[str] = ..., lease_generation: _Optional[int] = ..., task_epoch: _Optional[int] = ..., state: _Optional[str] = ..., reason: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ..., metrics: _Optional[_Mapping[str, float]] = ...) -> None: ...

class FaultReport(_message.Message):
    __slots__ = ("fault_key", "robot_name", "session_id", "revision_id", "reason", "latched", "local_response", "occurred_at_unix_us")
    FAULT_KEY_FIELD_NUMBER: _ClassVar[int]
    ROBOT_NAME_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    LATCHED_FIELD_NUMBER: _ClassVar[int]
    LOCAL_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    OCCURRED_AT_UNIX_US_FIELD_NUMBER: _ClassVar[int]
    fault_key: str
    robot_name: str
    session_id: str
    revision_id: str
    reason: _common_pb2.Reason
    latched: bool
    local_response: str
    occurred_at_unix_us: int
    def __init__(self, fault_key: _Optional[str] = ..., robot_name: _Optional[str] = ..., session_id: _Optional[str] = ..., revision_id: _Optional[str] = ..., reason: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ..., latched: _Optional[bool] = ..., local_response: _Optional[str] = ..., occurred_at_unix_us: _Optional[int] = ...) -> None: ...

class ComponentStatus(_message.Message):
    __slots__ = ("kind", "id", "phase", "reason", "observed_revision_id")
    KIND_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    kind: str
    id: str
    phase: str
    reason: _common_pb2.Reason
    observed_revision_id: str
    def __init__(self, kind: _Optional[str] = ..., id: _Optional[str] = ..., phase: _Optional[str] = ..., reason: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ..., observed_revision_id: _Optional[str] = ...) -> None: ...

class TelemetryBatch(_message.Message):
    __slots__ = ("schema_version", "sequence", "metrics")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    METRICS_FIELD_NUMBER: _ClassVar[int]
    schema_version: str
    sequence: int
    metrics: _containers.RepeatedCompositeFieldContainer[MetricPoint]
    def __init__(self, schema_version: _Optional[str] = ..., sequence: _Optional[int] = ..., metrics: _Optional[_Iterable[_Union[MetricPoint, _Mapping]]] = ...) -> None: ...

class MetricPoint(_message.Message):
    __slots__ = ("name", "labels", "value", "agent_time_us")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    NAME_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    AGENT_TIME_US_FIELD_NUMBER: _ClassVar[int]
    name: str
    labels: _containers.ScalarMap[str, str]
    value: float
    agent_time_us: int
    def __init__(self, name: _Optional[str] = ..., labels: _Optional[_Mapping[str, str]] = ..., value: _Optional[float] = ..., agent_time_us: _Optional[int] = ...) -> None: ...
