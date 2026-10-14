"""Validating observations against the active contract and decoding them into model inputs.

Everything a policy worker receives has passed these checks: protocol version, identity, digests,
tensor dtype/shape/finiteness, image dimensions and byte lengths, encoding, instruction length and
the rtc frozen prefix (design §12). Nothing is substituted or filled in.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from neura.v1 import common_pb2, dataplane_pb2
from neura_contracts import ColorOrder, ReasonCode
from neura_runtime import tensors
from neura_runtime.binding import ExecutionBinding

_COLOR = {ColorOrder.RGB: common_pb2.COLOR_ORDER_RGB, ColorOrder.BGR: common_pb2.COLOR_ORDER_BGR}


@dataclass(frozen=True, slots=True)
class ModelInputs:
    state: dict[str, NDArray[np.float32]]
    images: dict[str, NDArray[np.uint8]]  # camera role -> H x W x 3
    instruction: str
    frozen_prefix: NDArray[np.float32] | None


@dataclass(frozen=True, slots=True)
class InvalidObservation:
    reason: ReasonCode
    detail: str


def decode_observation(
    observation: dataplane_pb2.Observation, binding: ExecutionBinding
) -> ModelInputs | InvalidObservation:
    def invalid(detail: str) -> InvalidObservation:
        return InvalidObservation(ReasonCode.PROTOCOL_MESSAGE_INVALID, detail)

    state: dict[str, NDArray[np.float32]] = {}
    expected_state = {s.input_name: s for s in binding.state_layouts}
    if set(observation.state) != set(expected_state):
        return invalid(
            f"state inputs {sorted(observation.state)} do not match {sorted(expected_state)}"
        )
    for name, state_layout in expected_state.items():
        try:
            array = tensors.decode(
                observation.state[name], shape=(len(state_layout.feature_names),)
            )
        except tensors.TensorError as exc:
            return invalid(f"state {name!r}: {exc}")
        if not np.issubdtype(array.dtype, np.floating):
            return invalid(f"state {name!r} must be floating point")
        state[name] = array.astype(np.float32)

    images: dict[str, NDArray[np.uint8]] = {}
    selections = {e.camera_role: e for e in binding.profile.spec.image_encodings}
    cameras = {c.role: c for c in binding.robot.spec.cameras}
    received = {image.camera_role: image for image in observation.images}
    if len(received) != len(observation.images):
        return invalid("duplicate camera role")
    expected_roles = {layout.camera_role for layout in binding.image_layouts}
    if set(received) != expected_roles:
        return invalid(f"cameras {sorted(received)} do not match {sorted(expected_roles)}")
    for layout in binding.image_layouts:
        image = received[layout.camera_role]
        if (image.width, image.height) != (layout.width, layout.height):
            return invalid(f"camera {layout.camera_role!r} has wrong dimensions")
        if image.color_order != _COLOR[cameras[layout.camera_role].color_order]:
            return invalid(f"camera {layout.camera_role!r} has the wrong color order")
        selection = selections.get(layout.camera_role)
        if selection is None or selection.encoding.value != "raw":
            # Compressed encodings are decoded by model workers in a later milestone; until then
            # only raw frames are accepted, and anything else is refused rather than guessed.
            return invalid(f"camera {layout.camera_role!r}: only raw encoding is supported")
        if image.encoding != common_pb2.IMAGE_ENCODING_RAW:
            return invalid(f"camera {layout.camera_role!r}: encoding differs from the profile")
        if len(image.data) != layout.width * layout.height * 3:
            return invalid(f"camera {layout.camera_role!r}: byte length does not match size")
        images[layout.camera_role] = np.frombuffer(image.data, dtype=np.uint8).reshape(
            layout.height, layout.width, 3
        )

    instruction = observation.instruction
    if binding.language_input is not None:
        if not instruction:
            return invalid("the model requires an instruction")
        if len(instruction.encode("utf-8")) > binding.language_max_bytes:
            return invalid("instruction exceeds the execution profile's maximum length")
    elif instruction:
        return invalid("the model does not accept an instruction")

    frozen: NDArray[np.float32] | None = None
    if observation.HasField("frozen_prefix"):
        if binding.merge_mode.value != "rtc":
            return invalid("frozen_prefix is only valid for rtc merge mode")
        try:
            prefix = tensors.decode(observation.frozen_prefix)
        except tensors.TensorError as exc:
            return invalid(f"frozen_prefix: {exc}")
        merge = binding.profile.spec.merge
        limit = getattr(merge, "max_conditioned_delay_actions", 0)
        if prefix.ndim != 2 or prefix.shape[1] != binding.action_dim or prefix.shape[0] > limit:
            return invalid("frozen_prefix has the wrong shape")
        frozen = prefix.astype(np.float32)

    return ModelInputs(state=state, images=images, instruction=instruction, frozen_prefix=frozen)
