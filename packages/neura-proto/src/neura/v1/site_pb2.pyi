# GENERATED FILE: do not edit. Source: proto/neura/v1/site.proto. Regenerate with `make gen`.
from neura.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AgentMessage(_message.Message):
    __slots__ = ("hello", "ack", "status", "telemetry")
    HELLO_FIELD_NUMBER: _ClassVar[int]
    ACK_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TELEMETRY_FIELD_NUMBER: _ClassVar[int]
    hello: AgentHello
    ack: Acknowledgement
    status: SiteStatus
    telemetry: TelemetryBatch
    def __init__(self, hello: _Optional[_Union[AgentHello, _Mapping]] = ..., ack: _Optional[_Union[Acknowledgement, _Mapping]] = ..., status: _Optional[_Union[SiteStatus, _Mapping]] = ..., telemetry: _Optional[_Union[TelemetryBatch, _Mapping]] = ...) -> None: ...

class ControlPlaneMessage(_message.Message):
    __slots__ = ("desired_state", "command")
    DESIRED_STATE_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    desired_state: DesiredState
    command: Command
    def __init__(self, desired_state: _Optional[_Union[DesiredState, _Mapping]] = ..., command: _Optional[_Union[Command, _Mapping]] = ...) -> None: ...

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
    __slots__ = ("command_id", "prepare", "request_activation", "rollback", "collect_diagnostics")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    PREPARE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ACTIVATION_FIELD_NUMBER: _ClassVar[int]
    ROLLBACK_FIELD_NUMBER: _ClassVar[int]
    COLLECT_DIAGNOSTICS_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    prepare: PrepareRevision
    request_activation: RequestActivation
    rollback: Rollback
    collect_diagnostics: CollectDiagnostics
    def __init__(self, command_id: _Optional[str] = ..., prepare: _Optional[_Union[PrepareRevision, _Mapping]] = ..., request_activation: _Optional[_Union[RequestActivation, _Mapping]] = ..., rollback: _Optional[_Union[Rollback, _Mapping]] = ..., collect_diagnostics: _Optional[_Union[CollectDiagnostics, _Mapping]] = ...) -> None: ...

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
    __slots__ = ("agent_time_us", "components")
    AGENT_TIME_US_FIELD_NUMBER: _ClassVar[int]
    COMPONENTS_FIELD_NUMBER: _ClassVar[int]
    agent_time_us: int
    components: _containers.RepeatedCompositeFieldContainer[ComponentStatus]
    def __init__(self, agent_time_us: _Optional[int] = ..., components: _Optional[_Iterable[_Union[ComponentStatus, _Mapping]]] = ...) -> None: ...

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
