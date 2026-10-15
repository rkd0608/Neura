# GENERATED FILE: do not edit. Source: proto/neura/v1/worker.proto. Regenerate with `make gen`.
from neura.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class DescribeRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class WorkerDescription(_message.Message):
    __slots__ = ("artifact_digest", "adapter", "device", "precision", "deterministic", "batching", "multi_adapter", "parameters", "chunk_length", "action_dim", "pid")
    ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    ADAPTER_FIELD_NUMBER: _ClassVar[int]
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    PRECISION_FIELD_NUMBER: _ClassVar[int]
    DETERMINISTIC_FIELD_NUMBER: _ClassVar[int]
    BATCHING_FIELD_NUMBER: _ClassVar[int]
    MULTI_ADAPTER_FIELD_NUMBER: _ClassVar[int]
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    CHUNK_LENGTH_FIELD_NUMBER: _ClassVar[int]
    ACTION_DIM_FIELD_NUMBER: _ClassVar[int]
    PID_FIELD_NUMBER: _ClassVar[int]
    artifact_digest: str
    adapter: str
    device: str
    precision: str
    deterministic: bool
    batching: bool
    multi_adapter: bool
    parameters: int
    chunk_length: int
    action_dim: int
    pid: int
    def __init__(self, artifact_digest: _Optional[str] = ..., adapter: _Optional[str] = ..., device: _Optional[str] = ..., precision: _Optional[str] = ..., deterministic: _Optional[bool] = ..., batching: _Optional[bool] = ..., multi_adapter: _Optional[bool] = ..., parameters: _Optional[int] = ..., chunk_length: _Optional[int] = ..., action_dim: _Optional[int] = ..., pid: _Optional[int] = ...) -> None: ...

class WorkerInputs(_message.Message):
    __slots__ = ("state", "images", "instruction", "frozen_prefix")
    class StateEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _common_pb2.Tensor
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_common_pb2.Tensor, _Mapping]] = ...) -> None: ...
    class ImagesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _common_pb2.Tensor
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_common_pb2.Tensor, _Mapping]] = ...) -> None: ...
    STATE_FIELD_NUMBER: _ClassVar[int]
    IMAGES_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTION_FIELD_NUMBER: _ClassVar[int]
    FROZEN_PREFIX_FIELD_NUMBER: _ClassVar[int]
    state: _containers.MessageMap[str, _common_pb2.Tensor]
    images: _containers.MessageMap[str, _common_pb2.Tensor]
    instruction: str
    frozen_prefix: _common_pb2.Tensor
    def __init__(self, state: _Optional[_Mapping[str, _common_pb2.Tensor]] = ..., images: _Optional[_Mapping[str, _common_pb2.Tensor]] = ..., instruction: _Optional[str] = ..., frozen_prefix: _Optional[_Union[_common_pb2.Tensor, _Mapping]] = ...) -> None: ...

class InferRequest(_message.Message):
    __slots__ = ("batch_id", "inputs")
    BATCH_ID_FIELD_NUMBER: _ClassVar[int]
    INPUTS_FIELD_NUMBER: _ClassVar[int]
    batch_id: int
    inputs: _containers.RepeatedCompositeFieldContainer[WorkerInputs]
    def __init__(self, batch_id: _Optional[int] = ..., inputs: _Optional[_Iterable[_Union[WorkerInputs, _Mapping]]] = ...) -> None: ...

class InferResponse(_message.Message):
    __slots__ = ("batch_id", "chunks", "error", "compute_us", "peak_memory_mib")
    BATCH_ID_FIELD_NUMBER: _ClassVar[int]
    CHUNKS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    COMPUTE_US_FIELD_NUMBER: _ClassVar[int]
    PEAK_MEMORY_MIB_FIELD_NUMBER: _ClassVar[int]
    batch_id: int
    chunks: Chunks
    error: WorkerError
    compute_us: int
    peak_memory_mib: int
    def __init__(self, batch_id: _Optional[int] = ..., chunks: _Optional[_Union[Chunks, _Mapping]] = ..., error: _Optional[_Union[WorkerError, _Mapping]] = ..., compute_us: _Optional[int] = ..., peak_memory_mib: _Optional[int] = ...) -> None: ...

class Chunks(_message.Message):
    __slots__ = ("chunks",)
    CHUNKS_FIELD_NUMBER: _ClassVar[int]
    chunks: _containers.RepeatedCompositeFieldContainer[_common_pb2.Tensor]
    def __init__(self, chunks: _Optional[_Iterable[_Union[_common_pb2.Tensor, _Mapping]]] = ...) -> None: ...

class WorkerError(_message.Message):
    __slots__ = ("reason",)
    REASON_FIELD_NUMBER: _ClassVar[int]
    reason: _common_pb2.Reason
    def __init__(self, reason: _Optional[_Union[_common_pb2.Reason, _Mapping]] = ...) -> None: ...
