# GENERATED FILE: do not edit. Source: proto/neura/v1/common.proto. Regenerate with `make gen`.
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ProtocolVersion(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PROTOCOL_VERSION_UNSPECIFIED: _ClassVar[ProtocolVersion]
    PROTOCOL_VERSION_1: _ClassVar[ProtocolVersion]

class Dtype(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DTYPE_UNSPECIFIED: _ClassVar[Dtype]
    DTYPE_FLOAT64: _ClassVar[Dtype]
    DTYPE_FLOAT32: _ClassVar[Dtype]
    DTYPE_FLOAT16: _ClassVar[Dtype]
    DTYPE_BFLOAT16: _ClassVar[Dtype]
    DTYPE_INT64: _ClassVar[Dtype]
    DTYPE_INT32: _ClassVar[Dtype]
    DTYPE_UINT8: _ClassVar[Dtype]
    DTYPE_BOOL: _ClassVar[Dtype]

class ImageEncoding(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    IMAGE_ENCODING_UNSPECIFIED: _ClassVar[ImageEncoding]
    IMAGE_ENCODING_RAW: _ClassVar[ImageEncoding]
    IMAGE_ENCODING_JPEG: _ClassVar[ImageEncoding]
    IMAGE_ENCODING_PNG: _ClassVar[ImageEncoding]

class ColorOrder(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COLOR_ORDER_UNSPECIFIED: _ClassVar[ColorOrder]
    COLOR_ORDER_RGB: _ClassVar[ColorOrder]
    COLOR_ORDER_BGR: _ClassVar[ColorOrder]
PROTOCOL_VERSION_UNSPECIFIED: ProtocolVersion
PROTOCOL_VERSION_1: ProtocolVersion
DTYPE_UNSPECIFIED: Dtype
DTYPE_FLOAT64: Dtype
DTYPE_FLOAT32: Dtype
DTYPE_FLOAT16: Dtype
DTYPE_BFLOAT16: Dtype
DTYPE_INT64: Dtype
DTYPE_INT32: Dtype
DTYPE_UINT8: Dtype
DTYPE_BOOL: Dtype
IMAGE_ENCODING_UNSPECIFIED: ImageEncoding
IMAGE_ENCODING_RAW: ImageEncoding
IMAGE_ENCODING_JPEG: ImageEncoding
IMAGE_ENCODING_PNG: ImageEncoding
COLOR_ORDER_UNSPECIFIED: ColorOrder
COLOR_ORDER_RGB: ColorOrder
COLOR_ORDER_BGR: ColorOrder

class Envelope(_message.Message):
    __slots__ = ("protocol_version", "session_id", "lease_generation", "task_epoch", "sequence", "artifact_digest", "execution_profile_digest")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    LEASE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    TASK_EPOCH_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_DIGEST_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_PROFILE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    protocol_version: ProtocolVersion
    session_id: str
    lease_generation: int
    task_epoch: int
    sequence: int
    artifact_digest: str
    execution_profile_digest: str
    def __init__(self, protocol_version: _Optional[_Union[ProtocolVersion, str]] = ..., session_id: _Optional[str] = ..., lease_generation: _Optional[int] = ..., task_epoch: _Optional[int] = ..., sequence: _Optional[int] = ..., artifact_digest: _Optional[str] = ..., execution_profile_digest: _Optional[str] = ...) -> None: ...

class Tensor(_message.Message):
    __slots__ = ("dtype", "shape", "data")
    DTYPE_FIELD_NUMBER: _ClassVar[int]
    SHAPE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    dtype: Dtype
    shape: _containers.RepeatedScalarFieldContainer[int]
    data: bytes
    def __init__(self, dtype: _Optional[_Union[Dtype, str]] = ..., shape: _Optional[_Iterable[int]] = ..., data: _Optional[bytes] = ...) -> None: ...

class Image(_message.Message):
    __slots__ = ("camera_role", "width", "height", "color_order", "encoding", "quality", "data", "capture_time_us")
    CAMERA_ROLE_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    COLOR_ORDER_FIELD_NUMBER: _ClassVar[int]
    ENCODING_FIELD_NUMBER: _ClassVar[int]
    QUALITY_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_TIME_US_FIELD_NUMBER: _ClassVar[int]
    camera_role: str
    width: int
    height: int
    color_order: ColorOrder
    encoding: ImageEncoding
    quality: int
    data: bytes
    capture_time_us: int
    def __init__(self, camera_role: _Optional[str] = ..., width: _Optional[int] = ..., height: _Optional[int] = ..., color_order: _Optional[_Union[ColorOrder, str]] = ..., encoding: _Optional[_Union[ImageEncoding, str]] = ..., quality: _Optional[int] = ..., data: _Optional[bytes] = ..., capture_time_us: _Optional[int] = ...) -> None: ...

class Reason(_message.Message):
    __slots__ = ("code", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class PayloadDigest(_message.Message):
    __slots__ = ("sha256",)
    SHA256_FIELD_NUMBER: _ClassVar[int]
    sha256: bytes
    def __init__(self, sha256: _Optional[bytes] = ...) -> None: ...
