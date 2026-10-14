"""Encoding and validating ``neura.v1.Tensor`` payloads (row-major, little-endian)."""

from __future__ import annotations

import hashlib
import math

import numpy as np
from numpy.typing import NDArray

from neura.v1 import common_pb2

_TO_NUMPY: dict[common_pb2.Dtype, np.dtype[np.generic]] = {
    common_pb2.DTYPE_FLOAT64: np.dtype("<f8"),
    common_pb2.DTYPE_FLOAT32: np.dtype("<f4"),
    common_pb2.DTYPE_FLOAT16: np.dtype("<f2"),
    common_pb2.DTYPE_INT64: np.dtype("<i8"),
    common_pb2.DTYPE_INT32: np.dtype("<i4"),
    common_pb2.DTYPE_UINT8: np.dtype("u1"),
    common_pb2.DTYPE_BOOL: np.dtype("?"),
}
_FROM_NUMPY: dict[str, common_pb2.Dtype] = {
    dt.str.lstrip("<|"): code for code, dt in _TO_NUMPY.items()
}

# Upper bound on elements in any one tensor; a guard against hostile or corrupted shapes.
MAX_ELEMENTS = 64 * 1024 * 1024


class TensorError(ValueError):
    """A tensor payload is malformed or does not match what the receiver expects."""


def encode(array: NDArray[np.generic]) -> common_pb2.Tensor:
    contiguous = np.ascontiguousarray(array)
    key = contiguous.dtype.newbyteorder("<").str.lstrip("<|")
    code = _FROM_NUMPY.get(key)
    if code is None:
        raise TensorError(f"unsupported dtype {array.dtype}")
    little = contiguous.astype(_TO_NUMPY[code], copy=False)
    return common_pb2.Tensor(dtype=code, shape=list(little.shape), data=little.tobytes())


def decode(
    tensor: common_pb2.Tensor,
    *,
    shape: tuple[int, ...] | None = None,
    require_finite: bool = True,
) -> NDArray[np.generic]:
    """Decode a tensor, checking dtype, declared shape, byte length and (optionally) finiteness."""
    dtype = _TO_NUMPY.get(tensor.dtype)
    if dtype is None:
        raise TensorError(f"unsupported or unspecified dtype {tensor.dtype}")
    declared = tuple(int(d) for d in tensor.shape)
    if not declared:
        raise TensorError("tensor has no shape")
    elements = math.prod(declared)
    if elements > MAX_ELEMENTS:
        raise TensorError(f"tensor has {elements} elements, more than {MAX_ELEMENTS}")
    if shape is not None and declared != shape:
        raise TensorError(f"shape {declared} does not match expected {shape}")
    if len(tensor.data) != elements * dtype.itemsize:
        raise TensorError(f"{len(tensor.data)} bytes do not match shape {declared} of {dtype.name}")
    array = np.frombuffer(tensor.data, dtype=dtype).reshape(declared)
    if require_finite and np.issubdtype(dtype, np.floating) and not np.isfinite(array).all():
        raise TensorError("tensor contains NaN or infinity")
    return array


def payload_digest(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()
