"""Lint rules for api/openapi.yaml that encode the API conventions in design §20."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from openapi_spec_validator import validate

from neura_contracts import REASON_CODES, ReasonCode

SPEC_PATH = Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
SPEC: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
METHODS = {"get", "post", "put", "patch", "delete"}
# Read-only computations exposed as POST because they take a body; they must say so.
READ_ONLY_POSTS = {"checkCompatibility", "queryMetrics"}
UNAUTHENTICATED = {"getMeta"}


def _resolve(node: Any) -> Any:
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        assert ref.startswith("#/"), f"external reference {ref}"
        target: Any = SPEC
        for part in ref[2:].split("/"):
            target = target[part]
        node = target
    return node


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, item in SPEC["paths"].items()
        for method, operation in item.items()
        if method in METHODS
    ]


OPERATIONS = _operations()
IDS = [f"{m.upper()} {p}" for p, m, _ in OPERATIONS]


def _params(operation: dict[str, Any]) -> list[dict[str, Any]]:
    return [_resolve(p) for p in operation.get("parameters", [])]


def test_spec_is_valid_openapi_31() -> None:
    validate(SPEC)
    assert SPEC["openapi"].startswith("3.1")


def test_paths_are_versioned_and_kebab_case() -> None:
    for path in SPEC["paths"]:
        assert path.startswith("/v1/"), path
        for segment in path.split("/")[1:]:
            assert re.fullmatch(r"\{[a-z_]+\}|[a-z0-9]+(-[a-z0-9]+)*", segment), (path, segment)


def test_operation_ids_are_unique_camel_case() -> None:
    ids = [op["operationId"] for _, _, op in OPERATIONS]
    assert len(ids) == len(set(ids))
    for operation_id in ids:
        assert re.fullmatch(r"[a-z][A-Za-z0-9]+", operation_id), operation_id


@pytest.mark.parametrize(("path", "method", "operation"), OPERATIONS, ids=IDS)
def test_operation_conventions(path: str, method: str, operation: dict[str, Any]) -> None:
    declared_tags = {t["name"] for t in SPEC["tags"]}
    assert operation.get("summary"), "every operation needs a summary"
    assert operation.get("tags")
    assert set(operation["tags"]) <= declared_tags

    params = _params(operation)
    names = {(p["in"], p["name"]) for p in params}
    for placeholder in re.findall(r"\{([a-z_]+)\}", path):
        assert ("path", placeholder) in names, f"path parameter {placeholder} is not declared"

    responses = operation["responses"]
    operation_id = operation["operationId"]

    if method == "post" and operation_id not in READ_ONLY_POSTS:
        assert ("header", "Idempotency-Key") in names, "POST must require Idempotency-Key"
        assert "428" in responses
    if operation_id in READ_ONLY_POSTS:
        assert "Idempotency-Key" in operation.get("description", ""), (
            "read-only POSTs must document why no Idempotency-Key is needed"
        )
    if method in {"patch", "delete"}:
        assert ("header", "If-Match") in names, f"{method.upper()} must require If-Match"
        assert "412" in responses
    if method == "patch":
        assert "application/merge-patch+json" in operation["requestBody"]["content"]

    if operation_id in UNAUTHENTICATED:
        assert operation.get("security") == []
    else:
        assert "security" not in operation, "authenticated operations use the global security"
        for status in ("401", "403", "429", "500", "503"):
            assert status in responses, f"missing {status}"

    for status, response in responses.items():
        response = _resolve(response)
        if status.startswith(("4", "5")):
            content = response.get("content", {})
            assert "application/problem+json" in content, f"{status} must be a problem response"
        if status == "202":
            schema = response["content"]["application/json"]["schema"]
            assert schema == {"$ref": "#/components/schemas/Operation"}
            assert "Location" in response.get("headers", {})
        if status == "201":
            assert "Location" in response.get("headers", {})

    if ("query", "cursor") in names:
        assert "422" in responses, "operations with query parameters can reject them with 422"
    if operation_id.startswith("list"):
        assert ("query", "cursor") in names
        assert ("query", "page_size") in names
        schema = _resolve(responses["200"]["content"]["application/json"]["schema"])
        assert set(schema["required"]) == {"items", "page"}


def test_problem_responses_name_catalog_codes() -> None:
    for name, response in SPEC["components"]["responses"].items():
        code = ReasonCode(response["description"])
        info = REASON_CODES[code]
        assert info.area == "api", name


def test_status_is_read_only_on_resources() -> None:
    schemas = SPEC["components"]["schemas"]
    for name, schema in schemas.items():
        properties = schema.get("properties", {})
        status = properties.get("status")
        if status is None or "metadata" not in properties or name.endswith(("Create", "Update")):
            continue
        resolved = _resolve(status)
        inner = [_resolve(s) for s in resolved.get("allOf", [])] + [resolved]
        assert any(s.get("readOnly") for s in inner), f"{name}.status must be readOnly"


def test_create_and_update_bodies_cannot_set_status() -> None:
    for name, schema in SPEC["components"]["schemas"].items():
        if name.endswith(("Create", "Update")):
            assert "status" not in schema.get("properties", {}), name


def test_no_token_scope_can_approve_activation() -> None:
    scopes = SPEC["components"]["schemas"]["ApiTokenScope"]["enum"]
    assert not any("activat" in s or "approve" in s for s in scopes)


def test_identifier_schemas_use_typed_ulids() -> None:
    for name, schema in SPEC["components"]["schemas"].items():
        if name.endswith("Id") and schema.get("type") == "string":
            assert re.fullmatch(r"\^[a-z]+_\[0-9A-HJKMNP-TV-Z\]\{26\}\$", schema["pattern"]), name
