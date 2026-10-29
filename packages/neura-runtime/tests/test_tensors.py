"""Tensor payloads from the other side of a connection: a malformed one is a TensorError, never
another exception that would escape the receiver's validation."""

from __future__ import annotations

import numpy as np
import pytest

from neura.v1 import common_pb2
from neura_runtime import tensors


def test_a_tensor_round_trips() -> None:
    array = np.arange(6, dtype=np.float32).reshape(2, 3)
    assert (tensors.decode(tensors.encode(array)) == array).all()


@pytest.mark.parametrize("dims", [33, 70])
def test_a_shape_numpy_cannot_build_is_a_tensor_error(dims: int) -> None:
    tensor = common_pb2.Tensor(dtype=common_pb2.DTYPE_FLOAT32, shape=[1] * dims, data=b"\0" * 4)
    with pytest.raises(tensors.TensorError, match="dimensions"):
        tensors.decode(tensor)


def test_the_deepest_shape_allowed_decodes() -> None:
    shape = [1] * tensors.MAX_DIMS
    tensor = common_pb2.Tensor(dtype=common_pb2.DTYPE_UINT8, shape=shape, data=b"\x07")
    assert tensors.decode(tensor).shape == tuple(shape)
