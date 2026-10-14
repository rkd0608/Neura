"""Validates HTTP responses against api/openapi.yaml (spec-first conformance)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC_PATH = Path(__file__).resolve().parents[3] / "api" / "openapi.yaml"
SPEC_URI = "urn:neura:openapi"


def _pointer(*parts: str) -> str:
    return "/".join(p.replace("~", "~0").replace("/", "~1") for p in parts)


class SpecValidator:
    def __init__(self, path: Path = SPEC_PATH) -> None:
        self.spec: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        self._registry: Registry[Any] = Registry().with_resource(
            SPEC_URI, Resource.from_contents(self.spec, default_specification=DRAFT202012)
        )
        self._routes = []
        for template in self.spec["paths"]:
            pattern = "^" + re.sub(r"\{[a-z_]+\}", "[^/]+", template) + "$"
            self._routes.append((re.compile(pattern), template))
        self.seen: set[tuple[str, str, str]] = set()

    def template_for(self, path: str) -> str | None:
        matches = [t for regex, t in self._routes if regex.match(path)]
        # Prefer the most specific (fewest placeholders) template.
        return min(matches, key=lambda t: t.count("{"), default=None)

    def _resolve(self, node: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Follow $ref chains, returning the node and its JSON pointer parts."""
        parts: list[str] = []
        while "$ref" in node:
            ref = node["$ref"]
            parts = ref[2:].split("/")
            target: Any = self.spec
            for part in parts:
                target = target[part]
            node = target
        return node, parts

    def validate(self, method: str, path: str, status: int, content_type: str, body: bytes) -> None:
        template = self.template_for(path)
        if template is None:
            raise AssertionError(f"{method} {path}: no such path in the spec")
        operation = self.spec["paths"][template].get(method.lower())
        if operation is None:
            raise AssertionError(f"{method} {template}: no such operation in the spec")
        responses = operation["responses"]
        key = str(status)
        if key not in responses:
            raise AssertionError(f"{method} {template}: status {status} is not documented")
        self.seen.add((method.upper(), template, key))
        response, ref_parts = self._resolve(responses[key])
        content = response.get("content")
        if not content:
            assert not body, f"{method} {template} {status}: unexpected body"
            return
        media = content_type.split(";")[0].strip()
        if media not in content:
            raise AssertionError(f"{method} {template} {status}: undocumented media type {media}")
        if media == "text/event-stream":
            return
        if ref_parts:
            pointer = _pointer(*ref_parts, "content", media, "schema")
        else:
            pointer = _pointer(
                "paths", template, method.lower(), "responses", key, "content", media, "schema"
            )
        schema = {"$ref": f"{SPEC_URI}#/{pointer}"}
        validator = Draft202012Validator(
            schema, registry=self._registry, format_checker=FormatChecker()
        )
        instance = json.loads(body)
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
        if errors:
            details = "\n".join(f"  at {list(e.path)}: {e.message}" for e in errors[:10])
            raise AssertionError(
                f"{method} {template} {status} does not match the spec:\n{details}"
            )
