# GENERATED FILE: do not edit. Source: proto/neura/v1/capture.proto. Regenerate with `make gen`.
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CaptureMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CAPTURE_MODE_UNSPECIFIED: _ClassVar[CaptureMode]
    CAPTURE_MODE_OFF: _ClassVar[CaptureMode]
    CAPTURE_MODE_ALL: _ClassVar[CaptureMode]
    CAPTURE_MODE_FAILURES: _ClassVar[CaptureMode]
CAPTURE_MODE_UNSPECIFIED: CaptureMode
CAPTURE_MODE_OFF: CaptureMode
CAPTURE_MODE_ALL: CaptureMode
CAPTURE_MODE_FAILURES: CaptureMode

class CaptureSettings(_message.Message):
    __slots__ = ("mode", "endpoint", "image_stride", "cameras", "max_episode_seconds", "spool_max_bytes", "upload_bytes_per_second")
    MODE_FIELD_NUMBER: _ClassVar[int]
    ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    IMAGE_STRIDE_FIELD_NUMBER: _ClassVar[int]
    CAMERAS_FIELD_NUMBER: _ClassVar[int]
    MAX_EPISODE_SECONDS_FIELD_NUMBER: _ClassVar[int]
    SPOOL_MAX_BYTES_FIELD_NUMBER: _ClassVar[int]
    UPLOAD_BYTES_PER_SECOND_FIELD_NUMBER: _ClassVar[int]
    mode: CaptureMode
    endpoint: str
    image_stride: int
    cameras: _containers.RepeatedScalarFieldContainer[str]
    max_episode_seconds: int
    spool_max_bytes: int
    upload_bytes_per_second: int
    def __init__(self, mode: _Optional[_Union[CaptureMode, str]] = ..., endpoint: _Optional[str] = ..., image_stride: _Optional[int] = ..., cameras: _Optional[_Iterable[str]] = ..., max_episode_seconds: _Optional[int] = ..., spool_max_bytes: _Optional[int] = ..., upload_bytes_per_second: _Optional[int] = ...) -> None: ...

class EpisodeUpload(_message.Message):
    __slots__ = ("header", "data")
    HEADER_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    header: EpisodeHeader
    data: EpisodeData
    def __init__(self, header: _Optional[_Union[EpisodeHeader, _Mapping]] = ..., data: _Optional[_Union[EpisodeData, _Mapping]] = ...) -> None: ...

class EpisodeHeader(_message.Message):
    __slots__ = ("episode_id", "manifest")
    EPISODE_ID_FIELD_NUMBER: _ClassVar[int]
    MANIFEST_FIELD_NUMBER: _ClassVar[int]
    episode_id: str
    manifest: bytes
    def __init__(self, episode_id: _Optional[str] = ..., manifest: _Optional[bytes] = ...) -> None: ...

class EpisodeData(_message.Message):
    __slots__ = ("file", "data")
    FILE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    file: str
    data: bytes
    def __init__(self, file: _Optional[str] = ..., data: _Optional[bytes] = ...) -> None: ...

class EpisodeReceipt(_message.Message):
    __slots__ = ("episode_id", "stored")
    EPISODE_ID_FIELD_NUMBER: _ClassVar[int]
    STORED_FIELD_NUMBER: _ClassVar[int]
    episode_id: str
    stored: bool
    def __init__(self, episode_id: _Optional[str] = ..., stored: _Optional[bool] = ...) -> None: ...
