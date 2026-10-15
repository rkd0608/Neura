"""Generate the Python SDK's models and operations from api/openapi.yaml.

    uv run python tools/gen_sdk.py           write neura_sdk/models.py and neura_sdk/_operations.py
    uv run python tools/gen_sdk.py --check   fail if the committed output is stale

Models are TypedDicts (the API speaks JSON; nothing is converted), one per component schema, with
inline objects named after their parent. Each operation becomes a method on ``Operations``; the
hand-written ``Client`` supplies transport, idempotency keys, If-Match, retries and errors.
"""

from __future__ import annotations

import argparse
import keyword
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "api" / "openapi.yaml"
OUT = ROOT / "packages" / "neura-sdk" / "src" / "neura_sdk"
HEADER = '"""Generated from api/openapi.yaml by tools/gen_sdk.py. Do not edit."""\n'


def docstring(text: str, indent: str, width: int = 96) -> str:
    lines = textwrap.wrap(text, width=width - len(indent) - 3)
    if len(lines) <= 1:
        return f'{indent}"""{text}"""'
    body = "\n".join(f"{indent}{line}" for line in lines)
    return f'{indent}"""\n{body}\n{indent}"""'


def camel(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^0-9a-zA-Z]+", name) if part)


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class ModelWriter:
    def __init__(self, schemas: dict[str, Any]) -> None:
        self.schemas = schemas
        self.blocks: list[str] = []
        self.emitted: set[str] = set()

    def resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        while "$ref" in schema:
            schema = self.schemas[schema["$ref"].rsplit("/", 1)[1]]
        return schema

    def properties(self, schema: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
        """Properties and required names, merging allOf parts."""
        props: dict[str, Any] = {}
        required: set[str] = set()
        for part in schema.get("allOf", []):
            sub_props, sub_required = self.properties(self.resolve(part))
            props.update(sub_props)
            required |= sub_required
        props.update(schema.get("properties", {}))
        required |= set(schema.get("required", []))
        return props, required

    def annotation(self, schema: dict[str, Any], hint: str) -> str:
        if "$ref" in schema:
            return str(schema["$ref"]).rsplit("/", 1)[1]
        for key in ("oneOf", "anyOf"):
            # Beside properties, oneOf only constrains which are required (e.g. MemberCreate).
            if key in schema and "properties" not in schema:
                options = [
                    self.annotation(s, f"{hint}Option{i}") for i, s in enumerate(schema[key])
                ]
                return " | ".join(dict.fromkeys(options))
        if "const" in schema:
            return f"Literal[{schema['const']!r}]"
        if "enum" in schema:
            return "Literal[" + ", ".join(repr(v) for v in schema["enum"]) + "]"
        kind = schema.get("type")
        if isinstance(kind, list):
            parts = [self.annotation({**schema, "type": k}, hint) for k in kind]
            return " | ".join(dict.fromkeys(parts))
        if kind == "string":
            return "str"
        if kind == "integer":
            return "int"
        if kind == "number":
            return "float"
        if kind == "boolean":
            return "bool"
        if kind == "null":
            return "None"
        if kind == "array":
            if "prefixItems" in schema:
                items = [self.annotation(s, f"{hint}Item") for s in schema["prefixItems"]]
                return f"list[{' | '.join(dict.fromkeys(items))}]"
            return f"list[{self.annotation(schema.get('items', {}), f'{hint}Item')}]"
        if kind == "object" or "properties" in schema or "allOf" in schema:
            props, _ = self.properties(schema)
            if props:
                self.typed_dict(hint, schema)
                return hint
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict):
                return f"dict[str, {self.annotation(extra, f'{hint}Value')}]"
            return "dict[str, Any]"
        return "Any"

    def typed_dict(self, name: str, schema: dict[str, Any]) -> None:
        if name in self.emitted:
            return
        self.emitted.add(name)
        props, required = self.properties(schema)
        fields = []
        for prop, sub in props.items():
            annotation = self.annotation(sub, name + camel(prop))
            if prop not in required:
                annotation = f"NotRequired[{annotation}]"
            fields.append((prop, annotation, sub.get("description")))
        doc = (schema.get("description") or "").strip().replace('"""', "'''")
        functional = any(not p.isidentifier() or keyword.iskeyword(p) for p, _, _ in fields)
        if functional:
            body = ", ".join(f"{p!r}: {a}" for p, a, _ in fields)
            self.blocks.append(f"{name} = TypedDict({name!r}, {{{body}}})\n")
            return
        lines = [f"class {name}(TypedDict):"]
        if doc:
            lines.append(docstring(doc, "    ") + "\n")
        if not fields:
            lines.append("    pass")
        for prop, annotation, _ in fields:
            lines.append(f"    {prop}: {annotation}")
        self.blocks.append("\n".join(lines) + "\n")

    def write_all(self) -> str:
        for name, schema in self.schemas.items():
            resolved = schema
            props, _ = self.properties(resolved)
            if props:
                self.typed_dict(name, resolved)
            else:
                # A PEP 695 alias is evaluated lazily, so declaration order does not matter.
                self.blocks.append(f"type {name} = {self.annotation(resolved, name)}\n")
        self.names = [
            b.split()[1].split("(")[0] for b in self.blocks if b.startswith(("class ", "type "))
        ]
        return ""


def _escaped(path: str) -> str:
    """Interpolate path parameters escaped, so an id can never add a path segment."""
    return re.sub(r"\{(\w+)\}", r"{_segment(\1)}", path)


def operations(spec: dict[str, Any], models: ModelWriter) -> str:
    parameters = spec["components"]["parameters"]
    methods: list[str] = []
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method not in ("get", "post", "patch", "delete", "put"):
                continue
            responses = op["responses"]
            content_types = {
                ct
                for code, r in responses.items()
                if code.startswith("2")
                for ct in r.get("content", {})
            }
            if "text/event-stream" in content_types:
                continue  # streams are hand-written: Client.events()
            params = [
                parameters[p["$ref"].rsplit("/", 1)[1]] if "$ref" in p else p
                for p in op.get("parameters", [])
            ]
            path_params = [p for p in params if p["in"] == "path"]
            query_params = [p for p in params if p["in"] == "query"]
            headers = {p["name"] for p in params if p["in"] == "header"}
            signature = ["self"] + [f"{p['name']}: str" for p in path_params]
            keyword_args: list[str] = []
            body_type = None
            media_type = None
            if "requestBody" in op:
                media_type, media = next(iter(op["requestBody"]["content"].items()))
                body_type = models.annotation(media["schema"], camel(op["operationId"]) + "Body")
                keyword_args.append(f"body: {body_type}")
            if "If-Match" in headers:
                keyword_args.append("if_match: str")
            for p in query_params:
                kind = models.annotation(p["schema"], camel(op["operationId"]) + camel(p["name"]))
                keyword_args.append(f"{p['name']}: {kind} | None = None")
            if "Idempotency-Key" in headers:
                keyword_args.append("idempotency_key: str | None = None")
            if keyword_args:
                signature.append("*")
                signature.extend(keyword_args)
            result_types = []
            for code, r in sorted(responses.items()):
                if code.startswith("2") and "application/json" in r.get("content", {}):
                    schema = r["content"]["application/json"]["schema"]
                    result_types.append(
                        models.annotation(schema, camel(op["operationId"]) + "Result")
                    )
            returns = " | ".join(dict.fromkeys(result_types)) if result_types else "None"
            query = ", ".join(f"{p['name']!r}: {p['name']}" for p in query_params)
            call = [
                f"            {method.upper()!r},",
                f"            {'f' if '{' in path else ''}{_escaped(path)!r},",
            ]
            if query:
                call.append(f"            query={{{query}}},")
            if body_type is not None:
                call.append("            body=body,")
                call.append(f"            content_type={media_type!r},")
            if "If-Match" in headers:
                call.append("            if_match=if_match,")
            if "Idempotency-Key" in headers:
                call.append("            idempotency_key=idempotency_key,")
            summary = (op.get("summary") or op["operationId"]).strip()
            name = snake(op["operationId"])
            body = [
                f"    def {name}(",
                *[f"        {part}," for part in signature],
                f"    ) -> {returns}:",
                docstring(f"{summary} (``{method.upper()} {path}``)", "        "),
                "        return self._call(  # type: ignore[no-any-return]"
                if returns != "None"
                else "        self._call(",
                *call,
                "        )",
            ]
            methods.append("\n".join(body))
    text = "\n\n".join(methods)
    used = sorted(n for n in models.names if re.search(rf"\b{n}\b", text))
    imports = "".join(f"    {n},\n" for n in used)
    return (
        HEADER
        + "\nfrom __future__ import annotations\n\n"
        + "from abc import abstractmethod\n"
        + "from typing import Any\n"
        + "from urllib.parse import quote\n\n"
        + f"from neura_sdk.models import (\n{imports})\n\n\n"
        + "def _segment(value: str) -> str:\n"
        + '    """One path segment: every reserved character, including "/", escaped."""\n'
        + '    return quote(str(value), safe="")\n\n\n'
        + "class Operations:\n"
        + '    """One method per API operation. Bodies and results are plain JSON objects."""\n\n'
        + "    @abstractmethod\n"
        + "    def _call(\n"
        + "        self,\n"
        + "        method: str,\n"
        + "        path: str,\n"
        + "        *,\n"
        + "        query: dict[str, Any] | None = None,\n"
        + "        body: Any = None,\n"
        + "        content_type: str | None = None,\n"
        + "        if_match: str | None = None,\n"
        + "        idempotency_key: str | None = None,\n"
        + "    ) -> Any: ...\n\n"
        + text
        + "\n"
    )


def render() -> dict[str, str]:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    models = ModelWriter(spec["components"]["schemas"])
    models.write_all()
    ops = operations(spec, models)  # may add inline body/result models
    models.names = [
        b.split()[1].split("(")[0].split("=")[0]
        for b in models.blocks
        if b.startswith(("class ", "type "))
    ]
    ops = operations(spec, models)
    models_text = (
        HEADER
        + "\nfrom __future__ import annotations\n\n"
        + "from typing import Any, Literal, NotRequired, TypedDict\n\n\n"
        + "\n\n".join(models.blocks)
    )
    return {"models.py": models_text, "_operations.py": ops}


def formatted(files: dict[str, str]) -> dict[str, str]:
    """Run ruff format over the output so committed files are stable and readable."""
    out = {}
    for name, text in files.items():
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--stdin-filename", str(OUT / name), "-"],
            input=text,
            capture_output=True,
            text=True,
            check=True,
        )
        out[name] = result.stdout
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = formatted(render())
    stale = [n for n, t in files.items() if not (OUT / n).exists() or (OUT / n).read_text() != t]
    if args.check:
        if stale:
            print(f"stale SDK files: {stale}; run `make gen`", file=sys.stderr)
            return 1
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (OUT / name).write_text(text, encoding="utf-8")
    print(f"wrote {len(files)} files to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
