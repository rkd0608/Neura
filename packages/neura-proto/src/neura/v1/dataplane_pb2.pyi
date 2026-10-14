# GENERATED FILE: do not edit. Source: proto/neura/v1/dataplane.proto. Regenerate with `make gen`.
from neura.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class OpenSessionRequest(_message.Message):
    __slots__ = ("protocol_version", "robot_id", "revision_id", "artifact_digest", "execution_profile_digest", "robot_profile_digest")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    ROBOT_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_ID_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_PROFILE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    ROBOT_PROFILE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    protocol_version: _common_pb2.ProtocolVersion
    robot_id: str
    revision_id: str
    artifact_digest: str
    execution_profile_digest: str
    robot_profile_digest: str
    def __init__(self, protocol_version: _Optional[_Union[_common_pb2.ProtocolVersion, str]] = ..., robot_id: _Optional[str] = ..., revision_id: _Optional[str] = ..., artifact_digest: _Optional[str] = ..., execution_profile_digest: _Optional[str] = ..., robot_profile_digest: _Optional[str] = ...) -> None: ...

class OpenSessionResponse(_message.Message):
    __slots__ = ("accepted", "rejected")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REJECTED_FIELD_NUMBER: _ClassVar[int]
    accepted: SessionAccepted
    rejected: _common_pb2.Reason
    def __init__(self, accepted: _Optional[_Union[SessionAccepted, _Mapping]] = ..., rejected: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ...) -> None: ...

class SessionAccepted(_message.Message):
    __slots__ = ("session_id", "lease_generation", "lease_ttl_us", "max_message_bytes")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    LEASE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    LEASE_TTL_US_FIELD_NUMBER: _ClassVar[int]
    MAX_MESSAGE_BYTES_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    lease_generation: int
    lease_ttl_us: int
    max_message_bytes: int
    def __init__(self, session_id: _Optional[str] = ..., lease_generation: _Optional[int] = ..., lease_ttl_us: _Optional[int] = ..., max_message_bytes: _Optional[int] = ...) -> None: ...

class HeartbeatRequest(_message.Message):
    __slots__ = ("session_id", "lease_generation", "robot_send_time_us", "rtt_p99_us", "rtt_recent_us")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    LEASE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    ROBOT_SEND_TIME_US_FIELD_NUMBER: _ClassVar[int]
    RTT_P99_US_FIELD_NUMBER: _ClassVar[int]
    RTT_RECENT_US_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    lease_generation: int
    robot_send_time_us: int
    rtt_p99_us: int
    rtt_recent_us: int
    def __init__(self, session_id: _Optional[str] = ..., lease_generation: _Optional[int] = ..., robot_send_time_us: _Optional[int] = ..., rtt_p99_us: _Optional[int] = ..., rtt_recent_us: _Optional[int] = ...) -> None: ...

class HeartbeatResponse(_message.Message):
    __slots__ = ("ack", "rejected")
    ACK_FIELD_NUMBER: _ClassVar[int]
    REJECTED_FIELD_NUMBER: _ClassVar[int]
    ack: HeartbeatAck
    rejected: _common_pb2.Reason
    def __init__(self, ack: _Optional[_Union[HeartbeatAck, _Mapping]] = ..., rejected: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ...) -> None: ...

class HeartbeatAck(_message.Message):
    __slots__ = ("robot_send_time_us", "lease_remaining_us")
    ROBOT_SEND_TIME_US_FIELD_NUMBER: _ClassVar[int]
    LEASE_REMAINING_US_FIELD_NUMBER: _ClassVar[int]
    robot_send_time_us: int
    lease_remaining_us: int
    def __init__(self, robot_send_time_us: _Optional[int] = ..., lease_remaining_us: _Optional[int] = ...) -> None: ...

class ControlRequest(_message.Message):
    __slots__ = ("envelope", "epoch_advanced", "invalidate", "close")
    ENVELOPE_FIELD_NUMBER: _ClassVar[int]
    EPOCH_ADVANCED_FIELD_NUMBER: _ClassVar[int]
    INVALIDATE_FIELD_NUMBER: _ClassVar[int]
    CLOSE_FIELD_NUMBER: _ClassVar[int]
    envelope: _common_pb2.Envelope
    epoch_advanced: EpochAdvanced
    invalidate: Invalidate
    close: Close
    def __init__(self, envelope: _Optional[_Union[_common_pb2.Envelope, _Mapping]] = ..., epoch_advanced: _Optional[_Union[EpochAdvanced, _Mapping]] = ..., invalidate: _Optional[_Union[Invalidate, _Mapping]] = ..., close: _Optional[_Union[Close, _Mapping]] = ...) -> None: ...

class EpochAdvanced(_message.Message):
    __slots__ = ("previous_epoch", "cause")
    PREVIOUS_EPOCH_FIELD_NUMBER: _ClassVar[int]
    CAUSE_FIELD_NUMBER: _ClassVar[int]
    previous_epoch: int
    cause: _common_pb2.Reason
    def __init__(self, previous_epoch: _Optional[int] = ..., cause: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ...) -> None: ...

class Invalidate(_message.Message):
    __slots__ = ("cause",)
    CAUSE_FIELD_NUMBER: _ClassVar[int]
    cause: _common_pb2.Reason
    def __init__(self, cause: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ...) -> None: ...

class Close(_message.Message):
    __slots__ = ("cause",)
    CAUSE_FIELD_NUMBER: _ClassVar[int]
    cause: _common_pb2.Reason
    def __init__(self, cause: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ...) -> None: ...

class ControlResponse(_message.Message):
    __slots__ = ("ack", "rejected")
    ACK_FIELD_NUMBER: _ClassVar[int]
    REJECTED_FIELD_NUMBER: _ClassVar[int]
    ack: ControlAck
    rejected: _common_pb2.Reason
    def __init__(self, ack: _Optional[_Union[ControlAck, _Mapping]] = ..., rejected: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ...) -> None: ...

class ControlAck(_message.Message):
    __slots__ = ("task_epoch",)
    TASK_EPOCH_FIELD_NUMBER: _ClassVar[int]
    task_epoch: int
    def __init__(self, task_epoch: _Optional[int] = ...) -> None: ...

class RobotMessage(_message.Message):
    __slots__ = ("observation",)
    OBSERVATION_FIELD_NUMBER: _ClassVar[int]
    observation: Observation
    def __init__(self, observation: _Optional[_Union[Observation, _Mapping]] = ...) -> None: ...

class GatewayMessage(_message.Message):
    __slots__ = ("action_chunk", "rejected")
    ACTION_CHUNK_FIELD_NUMBER: _ClassVar[int]
    REJECTED_FIELD_NUMBER: _ClassVar[int]
    action_chunk: ActionChunk
    rejected: RequestRejected
    def __init__(self, action_chunk: _Optional[_Union[ActionChunk, _Mapping]] = ..., rejected: _Optional[_Union[RequestRejected, _Mapping]] = ...) -> None: ...

class Observation(_message.Message):
    __slots__ = ("envelope", "anchor_index", "queued_actions", "budget_us", "capture_time_us", "state", "images", "instruction", "frozen_prefix")
    class StateEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _common_pb2.Tensor
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_common_pb2.Tensor, _Mapping]] = ...) -> None: ...
    ENVELOPE_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_INDEX_FIELD_NUMBER: _ClassVar[int]
    QUEUED_ACTIONS_FIELD_NUMBER: _ClassVar[int]
    BUDGET_US_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_TIME_US_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    IMAGES_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTION_FIELD_NUMBER: _ClassVar[int]
    FROZEN_PREFIX_FIELD_NUMBER: _ClassVar[int]
    envelope: _common_pb2.Envelope
    anchor_index: int
    queued_actions: int
    budget_us: int
    capture_time_us: int
    state: _containers.MessageMap[str, _common_pb2.Tensor]
    images: _containers.RepeatedCompositeFieldContainer[_common_pb2.Image]
    instruction: str
    frozen_prefix: _common_pb2.Tensor
    def __init__(self, envelope: _Optional[_Union[_common_pb2.Envelope, _Mapping]] = ..., anchor_index: _Optional[int] = ..., queued_actions: _Optional[int] = ..., budget_us: _Optional[int] = ..., capture_time_us: _Optional[int] = ..., state: _Optional[_Mapping[str, _common_pb2.Tensor]] = ..., images: _Optional[_Iterable[_Union[_common_pb2.Image, _Mapping]]] = ..., instruction: _Optional[str] = ..., frozen_prefix: _Optional[_Union[_common_pb2.Tensor, _Mapping]] = ...) -> None: ...

class ActionChunk(_message.Message):
    __slots__ = ("envelope", "start_index", "actions", "actions_digest", "trace_id")
    ENVELOPE_FIELD_NUMBER: _ClassVar[int]
    START_INDEX_FIELD_NUMBER: _ClassVar[int]
    ACTIONS_FIELD_NUMBER: _ClassVar[int]
    ACTIONS_DIGEST_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    envelope: _common_pb2.Envelope
    start_index: int
    actions: _common_pb2.Tensor
    actions_digest: _common_pb2.PayloadDigest
    trace_id: str
    def __init__(self, envelope: _Optional[_Union[_common_pb2.Envelope, _Mapping]] = ..., start_index: _Optional[int] = ..., actions: _Optional[_Union[_common_pb2.Tensor, _Mapping]] = ..., actions_digest: _Optional[_Union[_common_pb2.PayloadDigest, _Mapping]] = ..., trace_id: _Optional[str] = ...) -> None: ...

class RequestRejected(_message.Message):
    __slots__ = ("envelope", "reason")
    ENVELOPE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    envelope: _common_pb2.Envelope
    reason: _common_pb2.Reason
    def __init__(self, envelope: _Optional[_Union[_common_pb2.Envelope, _Mapping]] = ..., reason: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ...) -> None: ...
