"""Loading contract documents from YAML or JSON text."""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import ValidationError

from neura_contracts.artifact import ModelArtifact
from neura_contracts.core import API_VERSION
from neura_contracts.execution import ExecutionProfile
from neura_contracts.reason_codes import ReasonCode
from neura_contracts.robot import RobotProfile

Document = ModelArtifact | RobotProfile | ExecutionProfile

KINDS: dict[str, type[ModelArtifact] | type[RobotProfile] | type[ExecutionProfile]] = {
    "ModelArtifact": ModelArtifact,
    "RobotProfile": RobotProfile,
    "ExecutionProfile": ExecutionProfile,
}


class ContractError(ValueError):
    """A document could not be loaded. ``errors`` lists each problem with its location."""

    def __init__(self, code: ReasonCode, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.errors = errors or []


def load_document(text: str) -> Document:
    """Parse and validate a contract document. The ``kind`` field selects the type."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractError(ReasonCode.CONTRACT_INVALID, f"not valid YAML or JSON: {exc}") from exc
    return parse_document(data)


def parse_document(data: Any) -> Document:
    if not isinstance(data, dict):
        raise ContractError(ReasonCode.CONTRACT_INVALID, "a document must be a mapping")
    api_version = data.get("api_version")
    if api_version != API_VERSION:
        raise ContractError(
            ReasonCode.CONTRACT_UNSUPPORTED_API_VERSION,
            f"unsupported api_version {api_version!r}; expected {API_VERSION!r}",
        )
    kind = data.get("kind")
    model = KINDS.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise ContractError(
            ReasonCode.CONTRACT_INVALID,
            f"unknown kind {kind!r}; expected one of {sorted(KINDS)}",
        )
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ContractError(
            ReasonCode.CONTRACT_INVALID, f"invalid {kind}: {len(errors)} error(s)", errors
        ) from exc


def dump_document(document: Document) -> str:
    """Serialize a document to YAML, omitting unset optional fields."""
    return yaml.safe_dump(document.canonical_data(), sort_keys=False, allow_unicode=True)
