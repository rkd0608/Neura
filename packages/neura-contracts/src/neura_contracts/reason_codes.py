"""Reason codes shared by every Neura component (design §20.5).

GENERATED FILE: do not edit. Source: catalog/reason_codes.yaml.
Regenerate with `make gen`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

Area = Literal["contracts", "gateway", "scheduler", "worker", "runtime", "agent", "api"]
Severity = Literal["info", "warning", "error", "fatal"]


class ReasonCode(StrEnum):
    CONTRACT_INVALID = "CONTRACT_INVALID"
    CONTRACT_UNSUPPORTED_API_VERSION = "CONTRACT_UNSUPPORTED_API_VERSION"
    COMPAT_ARTIFACT_DIGEST_MISMATCH = "COMPAT_ARTIFACT_DIGEST_MISMATCH"
    COMPAT_ROBOT_PROFILE_DIGEST_MISMATCH = "COMPAT_ROBOT_PROFILE_DIGEST_MISMATCH"
    COMPAT_ACTION_FEATURE_MISSING = "COMPAT_ACTION_FEATURE_MISSING"
    COMPAT_ACTION_FEATURE_UNUSED = "COMPAT_ACTION_FEATURE_UNUSED"
    COMPAT_ACTION_ORDER_MISMATCH = "COMPAT_ACTION_ORDER_MISMATCH"
    COMPAT_UNIT_MISMATCH = "COMPAT_UNIT_MISMATCH"
    COMPAT_FRAME_MISMATCH = "COMPAT_FRAME_MISMATCH"
    COMPAT_SEMANTICS_MISMATCH = "COMPAT_SEMANTICS_MISMATCH"
    COMPAT_STATE_FEATURE_MISSING = "COMPAT_STATE_FEATURE_MISSING"
    COMPAT_STATE_FEATURE_MISMATCH = "COMPAT_STATE_FEATURE_MISMATCH"
    COMPAT_CAMERA_MISSING = "COMPAT_CAMERA_MISSING"
    COMPAT_CAMERA_MISMATCH = "COMPAT_CAMERA_MISMATCH"
    COMPAT_IMAGE_ENCODING_UNSUPPORTED = "COMPAT_IMAGE_ENCODING_UNSUPPORTED"
    COMPAT_CONTROL_PERIOD_MISMATCH = "COMPAT_CONTROL_PERIOD_MISMATCH"
    COMPAT_CHUNK_SETTINGS_INVALID = "COMPAT_CHUNK_SETTINGS_INVALID"
    COMPAT_MERGE_MODE_NOT_VALIDATED = "COMPAT_MERGE_MODE_NOT_VALIDATED"
    COMPAT_FAULT_RESPONSE_NOT_VALIDATED = "COMPAT_FAULT_RESPONSE_NOT_VALIDATED"
    COMPAT_PRECISION_UNSUPPORTED = "COMPAT_PRECISION_UNSUPPORTED"
    COMPAT_STATEFUL_POLICY_UNSUPPORTED = "COMPAT_STATEFUL_POLICY_UNSUPPORTED"
    COMPAT_LANGUAGE_INPUT_UNSUPPORTED = "COMPAT_LANGUAGE_INPUT_UNSUPPORTED"
    COMPAT_TRANSFORM_INVALID = "COMPAT_TRANSFORM_INVALID"
    PROTOCOL_VERSION_MISMATCH = "PROTOCOL_VERSION_MISMATCH"
    PROTOCOL_MESSAGE_TOO_LARGE = "PROTOCOL_MESSAGE_TOO_LARGE"
    PROTOCOL_MESSAGE_INVALID = "PROTOCOL_MESSAGE_INVALID"
    AUTH_DEVICE_UNKNOWN = "AUTH_DEVICE_UNKNOWN"
    AUTH_DEVICE_REVOKED = "AUTH_DEVICE_REVOKED"
    LEASE_BUSY = "LEASE_BUSY"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    LEASE_GENERATION_STALE = "LEASE_GENERATION_STALE"
    ADMISSION_CAPACITY_EXCEEDED = "ADMISSION_CAPACITY_EXCEEDED"
    ADMISSION_MEMORY_EXCEEDED = "ADMISSION_MEMORY_EXCEEDED"
    DEPLOYMENT_NOT_ACTIVE = "DEPLOYMENT_NOT_ACTIVE"
    SESSION_UNKNOWN = "SESSION_UNKNOWN"
    SESSION_EPOCH_STALE = "SESSION_EPOCH_STALE"
    REQUEST_EXPIRED = "REQUEST_EXPIRED"
    REQUEST_SUPERSEDED = "REQUEST_SUPERSEDED"
    REQUEST_FENCED = "REQUEST_FENCED"
    WORKER_CRASHED = "WORKER_CRASHED"
    WORKER_OUT_OF_MEMORY = "WORKER_OUT_OF_MEMORY"
    WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"
    WORKER_OUTPUT_INVALID = "WORKER_OUTPUT_INVALID"
    WORKER_INFERENCE_FAILED = "WORKER_INFERENCE_FAILED"
    WORKER_LOAD_FAILED = "WORKER_LOAD_FAILED"
    WORKER_TIMED_OUT = "WORKER_TIMED_OUT"
    CHUNK_LATE = "CHUNK_LATE"
    CHUNK_STALE_OBSERVATION = "CHUNK_STALE_OBSERVATION"
    CHUNK_DUPLICATE = "CHUNK_DUPLICATE"
    CHUNK_EPOCH_MISMATCH = "CHUNK_EPOCH_MISMATCH"
    CHUNK_NOT_CONTIGUOUS = "CHUNK_NOT_CONTIGUOUS"
    CHUNK_INTEGRITY_FAILED = "CHUNK_INTEGRITY_FAILED"
    CHUNK_UNEXPECTED = "CHUNK_UNEXPECTED"
    REQUEST_TIMED_OUT = "REQUEST_TIMED_OUT"
    OBSERVATION_INCOMPLETE = "OBSERVATION_INCOMPLETE"
    CHUNK_REVISION_MISMATCH = "CHUNK_REVISION_MISMATCH"
    ACTION_OUT_OF_RANGE = "ACTION_OUT_OF_RANGE"
    BUFFER_STARVED = "BUFFER_STARVED"
    STARVATION_GRACE_EXHAUSTED = "STARVATION_GRACE_EXHAUSTED"
    CONTROLLER_COMMAND_FAILED = "CONTROLLER_COMMAND_FAILED"
    ACTUATOR_LOCK_LOST = "ACTUATOR_LOCK_LOST"
    OPERATOR_TAKEOVER = "OPERATOR_TAKEOVER"
    AGENT_CONTROL_PLANE_UNREACHABLE = "AGENT_CONTROL_PLANE_UNREACHABLE"
    AGENT_AUTHORIZATION_EXPIRING = "AGENT_AUTHORIZATION_EXPIRING"
    AGENT_AUTHORIZATION_EXPIRED = "AGENT_AUTHORIZATION_EXPIRED"
    AGENT_PREFLIGHT_FAILED = "AGENT_PREFLIGHT_FAILED"
    AGENT_MANIFEST_SIGNATURE_INVALID = "AGENT_MANIFEST_SIGNATURE_INVALID"
    AGENT_ARTIFACT_DIGEST_MISMATCH = "AGENT_ARTIFACT_DIGEST_MISMATCH"
    AGENT_ARTIFACT_UNAVAILABLE = "AGENT_ARTIFACT_UNAVAILABLE"
    AGENT_ARTIFACT_UNTRUSTED = "AGENT_ARTIFACT_UNTRUSTED"
    AGENT_ARTIFACT_SIGNATURE_INVALID = "AGENT_ARTIFACT_SIGNATURE_INVALID"
    AGENT_ARTIFACT_KEY_UNAVAILABLE = "AGENT_ARTIFACT_KEY_UNAVAILABLE"
    WORKER_IMAGE_UNAVAILABLE = "WORKER_IMAGE_UNAVAILABLE"
    AGENT_UPGRADE_REFUSED = "AGENT_UPGRADE_REFUSED"
    AGENT_UPGRADE_DEFERRED = "AGENT_UPGRADE_DEFERRED"
    AGENT_UPGRADE_ROLLED_BACK = "AGENT_UPGRADE_ROLLED_BACK"
    AGENT_DEVICE_UNAVAILABLE = "AGENT_DEVICE_UNAVAILABLE"
    AGENT_ENROLLMENT_INVALID = "AGENT_ENROLLMENT_INVALID"
    AGENT_UNAUTHENTICATED = "AGENT_UNAUTHENTICATED"
    AGENT_GENERATION_STALE = "AGENT_GENERATION_STALE"
    AGENT_ACTIVATION_REFUSED = "AGENT_ACTIVATION_REFUSED"
    AGENT_ACTIVATION_TIMED_OUT = "AGENT_ACTIVATION_TIMED_OUT"
    AGENT_BOUNDARY_DENIED = "AGENT_BOUNDARY_DENIED"
    AGENT_OPERATION_INTERRUPTED = "AGENT_OPERATION_INTERRUPTED"
    API_UNAUTHENTICATED = "API_UNAUTHENTICATED"
    API_FORBIDDEN = "API_FORBIDDEN"
    API_NOT_FOUND = "API_NOT_FOUND"
    API_CONFLICT = "API_CONFLICT"
    API_PRECONDITION_FAILED = "API_PRECONDITION_FAILED"
    API_IDEMPOTENCY_KEY_REQUIRED = "API_IDEMPOTENCY_KEY_REQUIRED"
    API_PRECONDITION_REQUIRED = "API_PRECONDITION_REQUIRED"
    API_IDEMPOTENCY_KEY_REUSED = "API_IDEMPOTENCY_KEY_REUSED"
    API_VALIDATION_FAILED = "API_VALIDATION_FAILED"
    API_PAYLOAD_TOO_LARGE = "API_PAYLOAD_TOO_LARGE"
    API_CURSOR_EXPIRED = "API_CURSOR_EXPIRED"
    API_RATE_LIMITED = "API_RATE_LIMITED"
    API_INTERNAL_ERROR = "API_INTERNAL_ERROR"
    API_UNAVAILABLE = "API_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ReasonCodeInfo:
    code: ReasonCode
    area: Area
    severity: Severity
    retryable: bool
    message: str
    http_status: int | None = None


REASON_CODES: dict[ReasonCode, ReasonCodeInfo] = {
    ReasonCode.CONTRACT_INVALID: ReasonCodeInfo(
        code=ReasonCode.CONTRACT_INVALID,
        area="contracts",
        severity="error",
        retryable=False,
        message='The document does not match its contract schema.',
    ),
    ReasonCode.CONTRACT_UNSUPPORTED_API_VERSION: ReasonCodeInfo(
        code=ReasonCode.CONTRACT_UNSUPPORTED_API_VERSION,
        area="contracts",
        severity="error",
        retryable=False,
        message="The document's api_version is not supported by this release.",
    ),
    ReasonCode.COMPAT_ARTIFACT_DIGEST_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.COMPAT_ARTIFACT_DIGEST_MISMATCH,
        area="contracts",
        severity="error",
        retryable=False,
        message='The execution profile is bound to a different model artifact.',
    ),
    ReasonCode.COMPAT_ROBOT_PROFILE_DIGEST_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.COMPAT_ROBOT_PROFILE_DIGEST_MISMATCH,
        area="contracts",
        severity="error",
        retryable=False,
        message='The execution profile is bound to a different robot profile.',
    ),
    ReasonCode.COMPAT_ACTION_FEATURE_MISSING: ReasonCodeInfo(
        code=ReasonCode.COMPAT_ACTION_FEATURE_MISSING,
        area="contracts",
        severity="error",
        retryable=False,
        message="The model outputs an action the robot's command interface does not accept.",
    ),
    ReasonCode.COMPAT_ACTION_FEATURE_UNUSED: ReasonCodeInfo(
        code=ReasonCode.COMPAT_ACTION_FEATURE_UNUSED,
        area="contracts",
        severity="error",
        retryable=False,
        message="The robot's command interface requires an action the model does not output.",
    ),
    ReasonCode.COMPAT_ACTION_ORDER_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.COMPAT_ACTION_ORDER_MISMATCH,
        area="contracts",
        severity="error",
        retryable=False,
        message='Action order differs between model and robot and no reorder transform is declared.',
    ),
    ReasonCode.COMPAT_UNIT_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.COMPAT_UNIT_MISMATCH,
        area="contracts",
        severity="error",
        retryable=False,
        message='Units differ between model and robot and no unit conversion is declared.',
    ),
    ReasonCode.COMPAT_FRAME_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.COMPAT_FRAME_MISMATCH,
        area="contracts",
        severity="error",
        retryable=False,
        message='Reference frames differ between model and robot.',
    ),
    ReasonCode.COMPAT_SEMANTICS_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.COMPAT_SEMANTICS_MISMATCH,
        area="contracts",
        severity="error",
        retryable=False,
        message='Action semantics differ between model and robot (for example absolute vs delta).',
    ),
    ReasonCode.COMPAT_STATE_FEATURE_MISSING: ReasonCodeInfo(
        code=ReasonCode.COMPAT_STATE_FEATURE_MISSING,
        area="contracts",
        severity="error",
        retryable=False,
        message='The model requires a state input the robot does not provide.',
    ),
    ReasonCode.COMPAT_STATE_FEATURE_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.COMPAT_STATE_FEATURE_MISMATCH,
        area="contracts",
        severity="error",
        retryable=False,
        message='A state input differs in unit, frame or shape between model and robot.',
    ),
    ReasonCode.COMPAT_CAMERA_MISSING: ReasonCodeInfo(
        code=ReasonCode.COMPAT_CAMERA_MISSING,
        area="contracts",
        severity="error",
        retryable=False,
        message='The model requires a camera the robot does not provide.',
    ),
    ReasonCode.COMPAT_CAMERA_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.COMPAT_CAMERA_MISMATCH,
        area="contracts",
        severity="error",
        retryable=False,
        message='A camera differs in resolution or color order between model and robot.',
    ),
    ReasonCode.COMPAT_IMAGE_ENCODING_UNSUPPORTED: ReasonCodeInfo(
        code=ReasonCode.COMPAT_IMAGE_ENCODING_UNSUPPORTED,
        area="contracts",
        severity="error",
        retryable=False,
        message='The selected image encoding is not accepted by the model or not produced by the robot.',
    ),
    ReasonCode.COMPAT_CONTROL_PERIOD_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.COMPAT_CONTROL_PERIOD_MISMATCH,
        area="contracts",
        severity="error",
        retryable=False,
        message='Control periods differ between model, robot and execution profile.',
    ),
    ReasonCode.COMPAT_CHUNK_SETTINGS_INVALID: ReasonCodeInfo(
        code=ReasonCode.COMPAT_CHUNK_SETTINGS_INVALID,
        area="contracts",
        severity="error",
        retryable=False,
        message='Chunk settings in the execution profile are inconsistent with the model.',
    ),
    ReasonCode.COMPAT_MERGE_MODE_NOT_VALIDATED: ReasonCodeInfo(
        code=ReasonCode.COMPAT_MERGE_MODE_NOT_VALIDATED,
        area="contracts",
        severity="error",
        retryable=False,
        message='The selected chunk merge mode is not validated for this model.',
    ),
    ReasonCode.COMPAT_FAULT_RESPONSE_NOT_VALIDATED: ReasonCodeInfo(
        code=ReasonCode.COMPAT_FAULT_RESPONSE_NOT_VALIDATED,
        area="contracts",
        severity="error",
        retryable=False,
        message='The selected starvation response is not validated for this robot.',
    ),
    ReasonCode.COMPAT_PRECISION_UNSUPPORTED: ReasonCodeInfo(
        code=ReasonCode.COMPAT_PRECISION_UNSUPPORTED,
        area="contracts",
        severity="error",
        retryable=False,
        message='The selected device or precision is not supported by the model.',
    ),
    ReasonCode.COMPAT_STATEFUL_POLICY_UNSUPPORTED: ReasonCodeInfo(
        code=ReasonCode.COMPAT_STATEFUL_POLICY_UNSUPPORTED,
        area="contracts",
        severity="error",
        retryable=False,
        message='Stateful policies are not supported by this release.',
    ),
    ReasonCode.COMPAT_LANGUAGE_INPUT_UNSUPPORTED: ReasonCodeInfo(
        code=ReasonCode.COMPAT_LANGUAGE_INPUT_UNSUPPORTED,
        area="contracts",
        severity="error",
        retryable=False,
        message='The model requires a language instruction the execution profile does not provide.',
    ),
    ReasonCode.COMPAT_TRANSFORM_INVALID: ReasonCodeInfo(
        code=ReasonCode.COMPAT_TRANSFORM_INVALID,
        area="contracts",
        severity="error",
        retryable=False,
        message='A declared adapter transform does not apply to the model and robot.',
    ),
    ReasonCode.PROTOCOL_VERSION_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.PROTOCOL_VERSION_MISMATCH,
        area="gateway",
        severity="error",
        retryable=False,
        message='The client and server protocol versions differ.',
    ),
    ReasonCode.PROTOCOL_MESSAGE_TOO_LARGE: ReasonCodeInfo(
        code=ReasonCode.PROTOCOL_MESSAGE_TOO_LARGE,
        area="gateway",
        severity="error",
        retryable=False,
        message='The message exceeds the configured maximum size.',
    ),
    ReasonCode.PROTOCOL_MESSAGE_INVALID: ReasonCodeInfo(
        code=ReasonCode.PROTOCOL_MESSAGE_INVALID,
        area="gateway",
        severity="error",
        retryable=False,
        message='The message failed schema, shape or finiteness validation.',
    ),
    ReasonCode.AUTH_DEVICE_UNKNOWN: ReasonCodeInfo(
        code=ReasonCode.AUTH_DEVICE_UNKNOWN,
        area="gateway",
        severity="error",
        retryable=False,
        message='The device credential is not enrolled at this site.',
    ),
    ReasonCode.AUTH_DEVICE_REVOKED: ReasonCodeInfo(
        code=ReasonCode.AUTH_DEVICE_REVOKED,
        area="gateway",
        severity="error",
        retryable=False,
        message='The device credential has been revoked.',
    ),
    ReasonCode.LEASE_BUSY: ReasonCodeInfo(
        code=ReasonCode.LEASE_BUSY,
        area="gateway",
        severity="warning",
        retryable=True,
        message='Another session holds the lease for this robot.',
    ),
    ReasonCode.LEASE_EXPIRED: ReasonCodeInfo(
        code=ReasonCode.LEASE_EXPIRED,
        area="gateway",
        severity="error",
        retryable=False,
        message="The session's lease expired; open a new session.",
    ),
    ReasonCode.LEASE_GENERATION_STALE: ReasonCodeInfo(
        code=ReasonCode.LEASE_GENERATION_STALE,
        area="gateway",
        severity="error",
        retryable=False,
        message='The message carries an old lease generation.',
    ),
    ReasonCode.ADMISSION_CAPACITY_EXCEEDED: ReasonCodeInfo(
        code=ReasonCode.ADMISSION_CAPACITY_EXCEEDED,
        area="gateway",
        severity="warning",
        retryable=True,
        message='Admitting this session would break the timing of sessions already admitted.',
    ),
    ReasonCode.ADMISSION_MEMORY_EXCEEDED: ReasonCodeInfo(
        code=ReasonCode.ADMISSION_MEMORY_EXCEEDED,
        area="gateway",
        severity="warning",
        retryable=True,
        message='Not enough GPU memory headroom to admit this session.',
    ),
    ReasonCode.DEPLOYMENT_NOT_ACTIVE: ReasonCodeInfo(
        code=ReasonCode.DEPLOYMENT_NOT_ACTIVE,
        area="gateway",
        severity="error",
        retryable=False,
        message='The requested deployment revision is not active at this site.',
    ),
    ReasonCode.SESSION_UNKNOWN: ReasonCodeInfo(
        code=ReasonCode.SESSION_UNKNOWN,
        area="gateway",
        severity="error",
        retryable=False,
        message='The session does not exist or has been closed.',
    ),
    ReasonCode.SESSION_EPOCH_STALE: ReasonCodeInfo(
        code=ReasonCode.SESSION_EPOCH_STALE,
        area="gateway",
        severity="info",
        retryable=False,
        message='The message carries a task epoch older than one the robot already announced.',
    ),
    ReasonCode.REQUEST_EXPIRED: ReasonCodeInfo(
        code=ReasonCode.REQUEST_EXPIRED,
        area="scheduler",
        severity="warning",
        retryable=False,
        message="The request's budget expired before it could be dispatched.",
    ),
    ReasonCode.REQUEST_SUPERSEDED: ReasonCodeInfo(
        code=ReasonCode.REQUEST_SUPERSEDED,
        area="scheduler",
        severity="info",
        retryable=False,
        message='A newer request from the same session replaced this one.',
    ),
    ReasonCode.REQUEST_FENCED: ReasonCodeInfo(
        code=ReasonCode.REQUEST_FENCED,
        area="scheduler",
        severity="info",
        retryable=False,
        message='The request belongs to an old task epoch or lease generation.',
    ),
    ReasonCode.WORKER_CRASHED: ReasonCodeInfo(
        code=ReasonCode.WORKER_CRASHED,
        area="worker",
        severity="error",
        retryable=False,
        message='The policy worker stopped while processing the request.',
    ),
    ReasonCode.WORKER_OUT_OF_MEMORY: ReasonCodeInfo(
        code=ReasonCode.WORKER_OUT_OF_MEMORY,
        area="worker",
        severity="error",
        retryable=False,
        message='The policy worker ran out of GPU memory.',
    ),
    ReasonCode.WORKER_UNAVAILABLE: ReasonCodeInfo(
        code=ReasonCode.WORKER_UNAVAILABLE,
        area="worker",
        severity="warning",
        retryable=True,
        message='No policy worker is available for this deployment.',
    ),
    ReasonCode.WORKER_OUTPUT_INVALID: ReasonCodeInfo(
        code=ReasonCode.WORKER_OUTPUT_INVALID,
        area="worker",
        severity="error",
        retryable=False,
        message='The policy produced non-finite or wrongly shaped actions.',
    ),
    ReasonCode.WORKER_INFERENCE_FAILED: ReasonCodeInfo(
        code=ReasonCode.WORKER_INFERENCE_FAILED,
        area="worker",
        severity="error",
        retryable=False,
        message='The policy raised an error for this batch.',
    ),
    ReasonCode.WORKER_LOAD_FAILED: ReasonCodeInfo(
        code=ReasonCode.WORKER_LOAD_FAILED,
        area="worker",
        severity="error",
        retryable=False,
        message='The policy worker could not load the model on its device.',
    ),
    ReasonCode.WORKER_TIMED_OUT: ReasonCodeInfo(
        code=ReasonCode.WORKER_TIMED_OUT,
        area="worker",
        severity="error",
        retryable=False,
        message='The policy worker did not finish the batch in time and was restarted.',
    ),
    ReasonCode.CHUNK_LATE: ReasonCodeInfo(
        code=ReasonCode.CHUNK_LATE,
        area="runtime",
        severity="warning",
        retryable=False,
        message='Too few useful actions remained after trimming the late chunk.',
    ),
    ReasonCode.CHUNK_STALE_OBSERVATION: ReasonCodeInfo(
        code=ReasonCode.CHUNK_STALE_OBSERVATION,
        area="runtime",
        severity="warning",
        retryable=False,
        message='The chunk was computed from an observation older than the freshness budget.',
    ),
    ReasonCode.CHUNK_DUPLICATE: ReasonCodeInfo(
        code=ReasonCode.CHUNK_DUPLICATE,
        area="runtime",
        severity="info",
        retryable=False,
        message='The chunk was already received and was ignored.',
    ),
    ReasonCode.CHUNK_EPOCH_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.CHUNK_EPOCH_MISMATCH,
        area="runtime",
        severity="info",
        retryable=False,
        message='The chunk belongs to a different task epoch.',
    ),
    ReasonCode.CHUNK_NOT_CONTIGUOUS: ReasonCodeInfo(
        code=ReasonCode.CHUNK_NOT_CONTIGUOUS,
        area="runtime",
        severity="error",
        retryable=False,
        message='The chunk would leave a gap between queued actions and its first action.',
    ),
    ReasonCode.CHUNK_INTEGRITY_FAILED: ReasonCodeInfo(
        code=ReasonCode.CHUNK_INTEGRITY_FAILED,
        area="runtime",
        severity="error",
        retryable=False,
        message="The chunk's payload digest, dtype or shape does not match its declaration.",
    ),
    ReasonCode.CHUNK_UNEXPECTED: ReasonCodeInfo(
        code=ReasonCode.CHUNK_UNEXPECTED,
        area="runtime",
        severity="info",
        retryable=False,
        message='The chunk answers a request that is no longer outstanding.',
    ),
    ReasonCode.REQUEST_TIMED_OUT: ReasonCodeInfo(
        code=ReasonCode.REQUEST_TIMED_OUT,
        area="runtime",
        severity="warning",
        retryable=True,
        message='No response arrived within the request timeout; the request was abandoned.',
    ),
    ReasonCode.OBSERVATION_INCOMPLETE: ReasonCodeInfo(
        code=ReasonCode.OBSERVATION_INCOMPLETE,
        area="runtime",
        severity="warning",
        retryable=True,
        message='A required sensor input was missing or too old, so no observation was sent.',
    ),
    ReasonCode.CHUNK_REVISION_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.CHUNK_REVISION_MISMATCH,
        area="runtime",
        severity="error",
        retryable=False,
        message='The chunk was computed by a revision that is not active on this robot.',
    ),
    ReasonCode.ACTION_OUT_OF_RANGE: ReasonCodeInfo(
        code=ReasonCode.ACTION_OUT_OF_RANGE,
        area="runtime",
        severity="fatal",
        retryable=False,
        message="An action is outside the robot's declared valid range.",
    ),
    ReasonCode.BUFFER_STARVED: ReasonCodeInfo(
        code=ReasonCode.BUFFER_STARVED,
        area="runtime",
        severity="warning",
        retryable=False,
        message='The action queue ran out; the approved starvation response is active.',
    ),
    ReasonCode.STARVATION_GRACE_EXHAUSTED: ReasonCodeInfo(
        code=ReasonCode.STARVATION_GRACE_EXHAUSTED,
        area="runtime",
        severity="fatal",
        retryable=False,
        message='No fresh actions arrived within the starvation grace period.',
    ),
    ReasonCode.CONTROLLER_COMMAND_FAILED: ReasonCodeInfo(
        code=ReasonCode.CONTROLLER_COMMAND_FAILED,
        area="runtime",
        severity="fatal",
        retryable=False,
        message='The robot controller rejected or failed to apply a command.',
    ),
    ReasonCode.ACTUATOR_LOCK_LOST: ReasonCodeInfo(
        code=ReasonCode.ACTUATOR_LOCK_LOST,
        area="runtime",
        severity="fatal",
        retryable=False,
        message='The runtime lost exclusive control of the robot controller.',
    ),
    ReasonCode.OPERATOR_TAKEOVER: ReasonCodeInfo(
        code=ReasonCode.OPERATOR_TAKEOVER,
        area="runtime",
        severity="info",
        retryable=False,
        message='An operator took control; policy execution authority was revoked.',
    ),
    ReasonCode.AGENT_CONTROL_PLANE_UNREACHABLE: ReasonCodeInfo(
        code=ReasonCode.AGENT_CONTROL_PLANE_UNREACHABLE,
        area="agent",
        severity="warning",
        retryable=True,
        message='The site agent cannot reach the control plane; the site continues on its committed configuration.',
    ),
    ReasonCode.AGENT_AUTHORIZATION_EXPIRING: ReasonCodeInfo(
        code=ReasonCode.AGENT_AUTHORIZATION_EXPIRING,
        area="agent",
        severity="warning",
        retryable=False,
        message="The site's offline authorization will expire soon.",
    ),
    ReasonCode.AGENT_AUTHORIZATION_EXPIRED: ReasonCodeInfo(
        code=ReasonCode.AGENT_AUTHORIZATION_EXPIRED,
        area="agent",
        severity="error",
        retryable=False,
        message="The site's offline authorization expired; new activations are blocked.",
    ),
    ReasonCode.AGENT_PREFLIGHT_FAILED: ReasonCodeInfo(
        code=ReasonCode.AGENT_PREFLIGHT_FAILED,
        area="agent",
        severity="error",
        retryable=True,
        message='A preflight check failed on this server.',
    ),
    ReasonCode.AGENT_MANIFEST_SIGNATURE_INVALID: ReasonCodeInfo(
        code=ReasonCode.AGENT_MANIFEST_SIGNATURE_INVALID,
        area="agent",
        severity="error",
        retryable=False,
        message='A desired-state manifest has a missing or invalid signature.',
    ),
    ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH: ReasonCodeInfo(
        code=ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH,
        area="agent",
        severity="error",
        retryable=False,
        message='A downloaded artifact does not match its declared digest.',
    ),
    ReasonCode.AGENT_ARTIFACT_UNAVAILABLE: ReasonCodeInfo(
        code=ReasonCode.AGENT_ARTIFACT_UNAVAILABLE,
        area="agent",
        severity="error",
        retryable=True,
        message="The model's weights are not available at this site; import them or configure a source.",
    ),
    ReasonCode.AGENT_ARTIFACT_UNTRUSTED: ReasonCodeInfo(
        code=ReasonCode.AGENT_ARTIFACT_UNTRUSTED,
        area="agent",
        severity="error",
        retryable=False,
        message='The model artifact carries no signature from a publisher key this server trusts.',
    ),
    ReasonCode.AGENT_ARTIFACT_SIGNATURE_INVALID: ReasonCodeInfo(
        code=ReasonCode.AGENT_ARTIFACT_SIGNATURE_INVALID,
        area="agent",
        severity="error",
        retryable=False,
        message='A signature on the model artifact from a trusted publisher key does not verify.',
    ),
    ReasonCode.AGENT_ARTIFACT_KEY_UNAVAILABLE: ReasonCodeInfo(
        code=ReasonCode.AGENT_ARTIFACT_KEY_UNAVAILABLE,
        area="agent",
        severity="error",
        retryable=True,
        message="The key that decrypts the model's sealed weights is not available to this server.",
    ),
    ReasonCode.WORKER_IMAGE_UNAVAILABLE: ReasonCodeInfo(
        code=ReasonCode.WORKER_IMAGE_UNAVAILABLE,
        area="worker",
        severity="error",
        retryable=True,
        message="The model's runtime image is not at this site and could not be pulled.",
    ),
    ReasonCode.AGENT_UPGRADE_REFUSED: ReasonCodeInfo(
        code=ReasonCode.AGENT_UPGRADE_REFUSED,
        area="agent",
        severity="error",
        retryable=False,
        message='The agent release was refused (not signed by a trusted release key, wrong digest or version, or not installable here).',
    ),
    ReasonCode.AGENT_UPGRADE_DEFERRED: ReasonCodeInfo(
        code=ReasonCode.AGENT_UPGRADE_DEFERRED,
        area="agent",
        severity="warning",
        retryable=True,
        message='The agent upgrade waited too long for every robot to be between tasks and was not applied.',
    ),
    ReasonCode.AGENT_UPGRADE_ROLLED_BACK: ReasonCodeInfo(
        code=ReasonCode.AGENT_UPGRADE_ROLLED_BACK,
        area="agent",
        severity="error",
        retryable=True,
        message='The new agent release did not come up healthy; the previous release runs again.',
    ),
    ReasonCode.AGENT_DEVICE_UNAVAILABLE: ReasonCodeInfo(
        code=ReasonCode.AGENT_DEVICE_UNAVAILABLE,
        area="agent",
        severity="error",
        retryable=True,
        message='No device this site may use matches the execution profile.',
    ),
    ReasonCode.AGENT_ENROLLMENT_INVALID: ReasonCodeInfo(
        code=ReasonCode.AGENT_ENROLLMENT_INVALID,
        area="agent",
        severity="error",
        retryable=False,
        message='The enrollment token is unknown, already used or expired.',
    ),
    ReasonCode.AGENT_UNAUTHENTICATED: ReasonCodeInfo(
        code=ReasonCode.AGENT_UNAUTHENTICATED,
        area="agent",
        severity="error",
        retryable=False,
        message="The agent's credential is invalid or its server is no longer enrolled.",
    ),
    ReasonCode.AGENT_GENERATION_STALE: ReasonCodeInfo(
        code=ReasonCode.AGENT_GENERATION_STALE,
        area="agent",
        severity="warning",
        retryable=False,
        message='The desired state is older than the one the site already committed.',
    ),
    ReasonCode.AGENT_ACTIVATION_REFUSED: ReasonCodeInfo(
        code=ReasonCode.AGENT_ACTIVATION_REFUSED,
        area="agent",
        severity="info",
        retryable=False,
        message='The site operator refused the activation.',
    ),
    ReasonCode.AGENT_ACTIVATION_TIMED_OUT: ReasonCodeInfo(
        code=ReasonCode.AGENT_ACTIVATION_TIMED_OUT,
        area="agent",
        severity="warning",
        retryable=False,
        message='Robots did not switch to the approved revision within the activation window.',
    ),
    ReasonCode.AGENT_BOUNDARY_DENIED: ReasonCodeInfo(
        code=ReasonCode.AGENT_BOUNDARY_DENIED,
        area="agent",
        severity="error",
        retryable=False,
        message='The instruction is outside the boundaries configured on this server.',
    ),
    ReasonCode.AGENT_OPERATION_INTERRUPTED: ReasonCodeInfo(
        code=ReasonCode.AGENT_OPERATION_INTERRUPTED,
        area="agent",
        severity="warning",
        retryable=True,
        message='The site agent restarted while the operation was running; run it again.',
    ),
    ReasonCode.API_UNAUTHENTICATED: ReasonCodeInfo(
        code=ReasonCode.API_UNAUTHENTICATED,
        area="api",
        severity="error",
        retryable=False,
        message='Authentication is required.',
        http_status=401,
    ),
    ReasonCode.API_FORBIDDEN: ReasonCodeInfo(
        code=ReasonCode.API_FORBIDDEN,
        area="api",
        severity="error",
        retryable=False,
        message='The caller does not have permission for this action.',
        http_status=403,
    ),
    ReasonCode.API_NOT_FOUND: ReasonCodeInfo(
        code=ReasonCode.API_NOT_FOUND,
        area="api",
        severity="error",
        retryable=False,
        message='The resource does not exist or is not visible to the caller.',
        http_status=404,
    ),
    ReasonCode.API_CONFLICT: ReasonCodeInfo(
        code=ReasonCode.API_CONFLICT,
        area="api",
        severity="error",
        retryable=False,
        message='The request conflicts with the current state of the resource.',
        http_status=409,
    ),
    ReasonCode.API_PRECONDITION_FAILED: ReasonCodeInfo(
        code=ReasonCode.API_PRECONDITION_FAILED,
        area="api",
        severity="error",
        retryable=False,
        message='The resource changed since it was read (If-Match did not match).',
        http_status=412,
    ),
    ReasonCode.API_IDEMPOTENCY_KEY_REQUIRED: ReasonCodeInfo(
        code=ReasonCode.API_IDEMPOTENCY_KEY_REQUIRED,
        area="api",
        severity="error",
        retryable=False,
        message='This request requires an Idempotency-Key header.',
        http_status=428,
    ),
    ReasonCode.API_PRECONDITION_REQUIRED: ReasonCodeInfo(
        code=ReasonCode.API_PRECONDITION_REQUIRED,
        area="api",
        severity="error",
        retryable=False,
        message='This request requires an If-Match header with the resource version.',
        http_status=428,
    ),
    ReasonCode.API_IDEMPOTENCY_KEY_REUSED: ReasonCodeInfo(
        code=ReasonCode.API_IDEMPOTENCY_KEY_REUSED,
        area="api",
        severity="error",
        retryable=False,
        message='The Idempotency-Key was already used with a different request body.',
        http_status=422,
    ),
    ReasonCode.API_VALIDATION_FAILED: ReasonCodeInfo(
        code=ReasonCode.API_VALIDATION_FAILED,
        area="api",
        severity="error",
        retryable=False,
        message='The request body failed validation.',
        http_status=422,
    ),
    ReasonCode.API_PAYLOAD_TOO_LARGE: ReasonCodeInfo(
        code=ReasonCode.API_PAYLOAD_TOO_LARGE,
        area="api",
        severity="error",
        retryable=False,
        message='The request body is larger than this operation accepts.',
        http_status=413,
    ),
    ReasonCode.API_CURSOR_EXPIRED: ReasonCodeInfo(
        code=ReasonCode.API_CURSOR_EXPIRED,
        area="api",
        severity="warning",
        retryable=False,
        message='The event cursor is older than the retention window; list resources again.',
        http_status=410,
    ),
    ReasonCode.API_RATE_LIMITED: ReasonCodeInfo(
        code=ReasonCode.API_RATE_LIMITED,
        area="api",
        severity="warning",
        retryable=True,
        message='Too many requests; retry after the indicated delay.',
        http_status=429,
    ),
    ReasonCode.API_INTERNAL_ERROR: ReasonCodeInfo(
        code=ReasonCode.API_INTERNAL_ERROR,
        area="api",
        severity="error",
        retryable=True,
        message='An internal error occurred.',
        http_status=500,
    ),
    ReasonCode.API_UNAVAILABLE: ReasonCodeInfo(
        code=ReasonCode.API_UNAVAILABLE,
        area="api",
        severity="error",
        retryable=True,
        message='The service is temporarily unavailable.',
        http_status=503,
    ),
}


def info(code: ReasonCode) -> ReasonCodeInfo:
    """Return the catalog entry for a reason code."""
    return REASON_CODES[code]
