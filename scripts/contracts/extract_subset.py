"""Extract a provider's request contract: a small OpenAPI 3.1 subset of the operations and fields our adapters send.

TEST_COVERAGE_SYSTEMIC_RFC §4a "Contracts", P17 (request side). The output is
`contracts/providers/<provider>/openapi.yaml`, written by this script only, never by hand;
`tests/contracts/openapi_contract.py` validates our requests against it.

What a subset holds, per operation in `PROVIDERS`:
- the top-level body fields we send (`Operation.fields`); every other field is pruned, so a new field fails the
  conformance tests until it is listed here and the subset regenerated;
- at each union in `Operation.variants`, only the variants we send, chosen by their `type` const (a variant without one:
  by its JSON type, e.g. "object", "string");
- `Operation.open` paths as free-form objects: they carry our own JSON Schemas (tool parameters, output formats);
- `Operation.allowed`: what our adapter sends although the source lacks it (or marks it deprecated), each injected with
  `x-alek-extension: <reason> (<date>)`. The run refuses to write when the source now has an allowed item: the entry is
  stale and must be removed.
Transforms (named in the file's `x-transforms`): see `_TRANSFORMS`.

Sources: an OpenAPI spec on GitHub pinned by commit (`Provider.repo`), or a Stainless SDK's generated request types
(`Provider.sdk`, pinned by the installed version; Anthropic publishes no reachable spec).

Usage (make contract-extract PROVIDER=openai|anthropic [REF=<sha>]):
    python -m scripts.contracts.extract_subset --provider openai [--ref <sha>]   # no ref: the default branch's head
    python -m scripts.contracts.extract_subset --provider anthropic              # the installed SDK
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib
import json
import subprocess
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import jsonschema
import pydantic
import typing_extensions
import yaml

from scripts.contracts.spec_drift import _pointer, fetch_url, normalize, parse

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "providers"
MAX_BYTES = 100_000


# ------------------------------------------------------------------------------------------------------- the table
@dataclass(frozen=True)
class Allowed:
    """Something our adapter sends that the source lacks or marks deprecated. Exactly one of three kinds:
    `value` (an enum literal, or the `type` of a union variant, at `path`), `field` + `schema` (a field the object at
    `path` lacks), `field` alone (a field of the object at `path` the source marks deprecated, which we keep
    sending)."""

    path: str                                  # body path ("" = the body) or "header:<name>"
    reason: str                                # the fact: what we send, where, why the source lacks it
    date: str                                  # when the gap was verified, YYYY-MM-DD
    value: str | None = None
    field: str | None = None
    schema: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Operation:
    """One operation and the parts of it we send. Paths: `a.b` is a property, `a[]` an array's items; unions are
    transparent (`input[].content[]` reaches the content list of every kept `input` variant)."""

    method: str                                         # "post"
    path: str                                           # as in the source; an SDK source names the REST path
    fields: tuple[str, ...]                             # the top-level body fields we send
    variants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)   # union path -> variants kept
    open: tuple[str, ...] = ()                          # free-form paths (our own JSON Schemas)
    allowed: tuple[Allowed, ...] = ()
    sdk_body: str = ""                                  # SDK source: the request TypedDict, "module:Name"
    headers: Mapping[str, str] = field(default_factory=dict)        # SDK source: header -> Literal type "module:Name"
    header_aliases: Mapping[str, str] = field(default_factory=dict)  # SDK source: body field -> its header alias


@dataclass(frozen=True)
class Provider:
    """A provider's request contract. Exactly one source: `repo` + `spec_file` (OpenAPI on GitHub) or `sdk`."""

    name: str
    operations: tuple[Operation, ...]
    repo: str = ""            # "openai/openai-openapi"
    spec_file: str = ""       # "openapi.yaml"
    sdk: str = ""             # "anthropic": the installed package and its version
    server: str = ""          # the base URL operation paths are relative to (an SDK source has no `servers`)


_OPENAI_ADAPTER = "src/adapters/openai_adapter.py"
_CLAUDE_ADAPTER = "src/adapters/claude_adapter.py"

PROVIDERS: dict[str, Provider] = {
    "openai": Provider(
        name="openai", repo="openai/openai-openapi", spec_file="openapi.yaml",
        operations=(Operation(
            # OpenAIAdapter.generate_content: client.responses.create (body built at :341-411)
            method="post", path="/responses",
            fields=("input", "instructions", "max_output_tokens", "model", "prompt_cache_key",
                    "prompt_cache_retention", "reasoning", "store", "text", "tool_choice", "tools"),
            variants={
                # user/assistant/developer messages, function calls and their outputs, and the output items of a
                # previous response passed back as they came (raw_content: message, reasoning, web_search_call)
                "input[]": ("message", "function_call", "function_call_output", "reasoning", "web_search_call"),
                "input[].content[]": ("input_text", "input_image", "input_file", "output_text", "reasoning_text"),
                "tools[]": ("function", "web_search"),
                "tool_choice": ("string",),              # "auto" | "required"
                "reasoning": ("object",),
                "text.format": ("json_schema", "json_object"),
            },
            open=("tools[].parameters", "text.format.schema"),
            allowed=(Allowed(
                path="", field="prompt_cache_retention", date="2026-11-18",
                reason=f"deprecated in the spec; sent only for models before GPT-5.6, where it still sets the 24h "
                       f"retention ({_OPENAI_ADAPTER} _DEPRECATED_RETENTION_PREFIXES, generate_content)",
            ),),
        ),),
    ),
    "anthropic": Provider(
        name="anthropic", sdk="anthropic", server="https://api.anthropic.com",
        operations=(Operation(
            # ClaudeAdapter.generate_content and _grounded_stream_loop: client.messages.stream
            method="post", path="/v1/messages",
            sdk_body="anthropic.types.message_create_params:MessageCreateParamsStreaming",
            fields=("container", "max_tokens", "messages", "model", "output_config", "stream", "system",
                    "thinking", "tool_choice", "tools"),
            variants={
                # our own blocks, and an assistant turn passed back as it came (raw_content; the grounded loop's
                # pause_turn continuation carries the server tools' blocks)
                "messages[].content[]": (
                    "text", "image", "document", "tool_use", "tool_result", "thinking", "redacted_thinking",
                    "server_tool_use", "web_search_tool_result", "web_fetch_tool_result",
                    "code_execution_tool_result", "bash_code_execution_tool_result",
                    "text_editor_code_execution_tool_result",
                ),
                "tools[]": ("custom", "web_search_20260209", "web_fetch_20260209", "web_search_20250305",
                            "web_fetch_20250910"),
                "thinking": ("adaptive", "disabled"),
                "tool_choice": ("auto", "any"),
                "output_config.format": ("json_schema",),
                "system[]": ("text",),
            },
            open=("tools[].input_schema", "output_config.format.schema"),
            headers={"anthropic-beta": "anthropic.types.anthropic_beta_param:AnthropicBetaParam"},
            header_aliases={"user_profile_id": "anthropic-user-profile-id"},
            allowed=(
                Allowed(
                    path="thinking", value="between_tools", date="2026-11-18",
                    reason=f"Sonnet 5.5 rejects thinking.type=disabled; between_tools is its no-thinking floor "
                           f"({_CLAUDE_ADAPTER} _THINKING_OFF), absent from the SDK's ThinkingConfigParam",
                ),
                Allowed(
                    path="header:anthropic-beta", value="web-search-2025-03-05", date="2026-11-18",
                    reason=f"sent with the legacy web_search_20250305 tool on the grounding path of models without "
                           f"dynamic search ({_CLAUDE_ADAPTER} generate_content beta_headers), absent from the SDK's "
                           f"AnthropicBetaParam literals",
                ),
            ),
        ),),
    ),
}

_TRANSFORMS = (
    "prune: only the listed top-level fields and union variants are kept",
    "flatten-allOf: an allOf of objects becomes one object (properties and required merged)",
    "oneOf-to-anyOf: variants overlap (a message with list content matches two of them)",
    "close-objects: additionalProperties false on every object that declares properties, except the open paths",
    "open-paths: our own JSON Schemas (the open paths) are free-form objects",
    "drop-redundant-literals: a literal next to a plain {type: T} in a union accepts nothing more (model id lists)",
    "drop-doc-keys: documentation keys and vendor doc extensions dropped (spec_drift.normalize); property names kept",
    "stringify-dates: YAML dates become ISO strings",
)
_SDK_TRANSFORMS = (
    "sdk-schema: pydantic TypeAdapter(<request TypedDict>).json_schema(), $defs as components",
    "drop-header-aliases: fields the SDK sends as headers (PropertyInfo alias) are not body fields",
    "drop-sdk-response-models: union members that are SDK response models (accepted as input for convenience) are "
    "dropped; the request types (*Param) are the wire contract",
    "header-literals: a header's allowed values are its SDK type's Literal values",
)


# ------------------------------------------------------------------------------------------------------ the subset
_REF_PREFIX = "#/components/schemas/"
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_ONE = ("items", "not", "contains", "if", "then", "else", "propertyNames")
_DROPPED = frozenset({"discriminator", "$schema", "$id"})


def _plain(value: Any) -> Any:
    """YAML dates as ISO strings, recursively."""
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _join(path: str, step: str) -> str:
    if step == "[]":
        return path + "[]"
    return f"{path}.{step}" if path else step


class _Builder:
    """Materialises one operation's body schema from a source spec: path-dependent pruning, refs kept as components."""

    def __init__(self, spec: dict, op: Operation):
        self.spec = spec
        self.op = op
        self.components: dict[str, Any] = {}
        self._names: dict[tuple[str, str], str] = {}
        self._stack: list[str] = []
        self.reached: set[str] = set()
        self._found: dict[str, set[str]] = {}
        self._allowed = {a.path: [] for a in op.allowed}
        for a in op.allowed:
            self._allowed[a.path].append(a)

    # -- source navigation
    def resolve(self, node: Any) -> Any:
        seen = 0
        while isinstance(node, dict) and "$ref" in node:
            node = _pointer(self.spec, node["$ref"])
            seen += 1
            if seen > 50:
                raise ValueError("a $ref chain does not end")
        return node

    def merged(self, node: Any) -> dict:
        """The resolved node with an allOf of objects flattened into one object (recursively)."""
        node = self.resolve(node)
        if not isinstance(node, dict) or "allOf" not in node:
            return node
        out = {k: v for k, v in node.items() if k != "allOf"}
        props: dict[str, Any] = dict(out.get("properties") or {})
        required: list[str] = list(out.get("required") or [])
        for member in node["allOf"]:
            part = self.merged(member)
            if not isinstance(part, dict) or not (part.get("type") == "object" or "properties" in part):
                raise ValueError(f"allOf member is not an object: {_canon(part)[:200]}")
            if part.get("anyOf") or part.get("oneOf"):
                raise ValueError("allOf member with its own union: not flattenable")
            props.update(part.get("properties") or {})
            required += [r for r in part.get("required") or [] if r not in required]
            for key, value in part.items():
                if key not in ("properties", "required", "type") and key not in out:
                    out[key] = value
        out["type"] = "object"
        out["properties"] = props
        if required:
            out["required"] = required
        return out

    def members(self, node: Any) -> list[Any]:
        """A union's leaf members (nested unions flattened); each member as written (a $ref stays a $ref)."""
        resolved = self.merged(node)
        union = resolved.get("anyOf") or resolved.get("oneOf") if isinstance(resolved, dict) else None
        if not union:
            return [node]
        if "properties" in resolved:
            raise ValueError(f"a union next to properties: {_canon(resolved)[:200]}")
        out: list[Any] = []
        for member in union:
            out += self.members(member)
        return out

    def type_values(self, node: Any) -> set[str] | None:
        """The `type` property's const/enum values of an object schema, or None when it has no such tag."""
        resolved = self.merged(node)
        if not isinstance(resolved, dict):
            return None
        tag = (resolved.get("properties") or {}).get("type")
        if tag is None:
            return None
        values: set[str] = set()
        for part in self.members(tag):
            part = self.resolve(part)
            if "const" in part:
                values.add(part["const"])
            values.update(v for v in part.get("enum") or [] if isinstance(v, str))
        return values or None

    def selector(self, node: Any) -> set[str]:
        tags = self.type_values(node)
        if tags:
            return tags
        resolved = self.merged(node)
        kind = resolved.get("type") if isinstance(resolved, dict) else None
        if kind is None and isinstance(resolved, dict) and "properties" in resolved:
            kind = "object"
        return set(kind) if isinstance(kind, list) else {kind} if kind else set()

    # -- materialisation
    def body(self, node: Any) -> Any:
        self._check_paths()
        root = self.build(node, "")
        body_allowed = (a.path for a in self.op.allowed if not a.path.startswith("header:"))
        missing = sorted({*self.op.variants, *self.op.open, *body_allowed} - self.reached)
        if missing:
            raise ValueError(f"paths never reached (typo, or the source moved them): {missing}")
        for path, wanted_variants in self.op.variants.items():
            absent = sorted(set(wanted_variants) - self._found.get(path, set()))
            if absent:
                raise ValueError(f"variants absent at {path}: {absent}")
        return root

    def _check_paths(self) -> None:
        for path in [*self.op.variants, *self.op.open, *(a.path for a in self.op.allowed)]:
            if path.startswith("header:"):
                if path[len("header:"):] not in self.op.headers:
                    raise ValueError(f"allowed header {path!r} is not a declared header")
                continue
            head = path.split(".")[0].split("[")[0]
            if path and head not in self.op.fields:
                raise ValueError(f"path {path!r} is under no listed field")

    def build(self, node: Any, path: str, *, whole: bool = True) -> Any:
        """The materialised schema at `path`. `whole` is False for a member of the union at `path` (or a resolved
        $ref): the variant selection and an ALLOWED value apply once, to the whole node at the path."""
        self.reached.add(path)
        if path in self.op.open:
            built: Any = {"type": "object"}
        elif whole and path in self.op.variants:
            built = self._variants(node, path)
        elif isinstance(node, dict) and "$ref" in node:
            built = self._component(node["$ref"], path)
        else:
            built = self._schema(self.merged(node), path)
        return self._inject_values(built, path) if whole else built

    def _component(self, ref: str, path: str) -> Any:
        name = ref.rsplit("/", 1)[-1]
        if name in self._stack:
            raise ValueError(f"recursive schema {name} at {path or '<body>'}: prune it or declare the path open")
        self._stack.append(name)
        try:
            built = self.build(self.resolve({"$ref": ref}), path, whole=False)
        finally:
            self._stack.pop()
        key = (name, _canon(built))
        if key not in self._names:
            cname, n = name, 1
            while cname in self.components:
                n += 1
                cname = f"{name}_{n}"
            self._names[key] = cname
            self.components[cname] = built
        return {"$ref": _REF_PREFIX + self._names[key]}

    def _variants(self, node: Any, path: str) -> Any:
        wanted = self.op.variants[path]
        members = self.members(node)
        by_name = [(self.selector(m), m) for m in members]
        available = set().union(*(names for names, _ in by_name)) if by_name else set()
        kept = [m for names, m in by_name if names & set(wanted)]
        injected = [a for a in self._allowed.get(path, []) if a.value is not None]
        for a in injected:
            if a.value in available:
                raise ValueError(f"stale ALLOWED at {path}: the source now has variant {a.value!r}; remove the entry")
        # A path can reach several unions (the content lists of several message variants): each keeps what it has
        # of the wanted variants, and every wanted variant must exist at one of them at least (checked in `body`).
        self._found.setdefault(path, set()).update(available & set(wanted))
        if not kept and not injected:
            raise ValueError(f"no wanted variant at a union of {path} (it has {sorted(available)})")
        built = [self.build(m, path, whole=False) for m in kept]
        built += [{
            "type": "object", "properties": {"type": {"const": a.value}}, "required": ["type"],
            "additionalProperties": False, "x-alek-extension": _note(a),
        } for a in injected]
        return built[0] if len(built) == 1 else {"anyOf": built}

    def _schema(self, node: Any, path: str) -> Any:
        if not isinstance(node, dict):
            return _plain(node)
        out: dict[str, Any] = {}
        for key in sorted(node):
            value = node[key]
            if key in _DROPPED:
                continue
            if key == "properties":
                out[key] = self._properties(value, path)
            elif key in ("anyOf", "oneOf"):
                out["anyOf"] = [self.build(m, path, whole=False) for m in value]
            elif key in _SCHEMA_LISTS:
                raise ValueError(f"{key} at {path or '<body>'}: not flattened")
            elif key == "items":
                out[key] = self.build(value, _join(path, "[]"))
            elif key in _SCHEMA_ONE or key == "additionalProperties" and isinstance(value, dict):
                out[key] = self.build(value, _join(path, "*"))
            else:
                kept = normalize({key: _plain(value)})
                out.update(kept)
        if "properties" in out:
            if any("properties" in self.merged(m) for m in node.get("anyOf") or node.get("oneOf") or []):
                raise ValueError(f"object with a union of objects at {path or '<body>'}: cannot be closed")
            out.setdefault("additionalProperties", False)
        if path == "":
            self._check_root_required(out)
        if "anyOf" in out:
            out["anyOf"] = self._without_redundant_literals(out["anyOf"])
            if len(out["anyOf"]) == 1 and len(out) == 1:
                return out["anyOf"][0]
        return out

    def _lookup(self, node: Any) -> Any:
        while isinstance(node, dict) and "$ref" in node:
            node = self.components[node["$ref"].rsplit("/", 1)[-1]]
        return node

    def _without_redundant_literals(self, members: list[Any]) -> list[Any]:
        """Drop a literal member (`enum`/`const` of type T) when a plain `{type: T}` is in the same union: it accepts
        nothing more (the providers' model id lists, kilobytes of names next to "any string")."""
        plain = {r["type"] for r in map(self._lookup, members)
                 if isinstance(r, dict) and set(r) == {"type"} and isinstance(r["type"], str)}

        def redundant(member: Any) -> bool:
            r = self._lookup(member)
            keys = {k for k in r if not k.startswith("x-")} if isinstance(r, dict) else set()
            return (isinstance(r, dict) and r.get("type") in plain and bool(keys & {"enum", "const"})
                    and keys <= {"type", "enum", "const", "default"})

        return [m for m in members if not redundant(m)]

    def _properties(self, props: dict, path: str) -> dict:
        names = sorted(props)
        if path == "":
            absent = [f for f in self.op.fields if f not in props
                      and not any(a.path == "" and a.field == f and a.schema is not None for a in self.op.allowed)]
            if absent:
                raise ValueError(f"listed fields absent from the source: {absent}")
            names = [n for n in names if n in self.op.fields]
        out = {name: self.build(props[name], _join(path, name)) for name in names}
        for a in self._allowed.get(path, []):
            if a.field is None:
                continue
            if a.schema is not None:
                if a.field in props:
                    raise ValueError(f"stale ALLOWED at {path or '<body>'}: the source now has field {a.field!r}")
                out[a.field] = {**_plain(dict(a.schema)), "x-alek-extension": _note(a)}
            else:
                raw = props.get(a.field)
                if raw is None:
                    raise ValueError(f"ALLOWED deprecated field {a.field!r} is absent from the source")
                if not (isinstance(raw, dict) and raw.get("deprecated")):
                    raise ValueError(f"stale ALLOWED at {path or '<body>'}: {a.field!r} is no longer deprecated")
                built = out[a.field]
                if "$ref" in built:
                    raise ValueError(f"deprecated field {a.field!r} is a shared component: unsupported")
                built["x-alek-extension"] = _note(a)
        return dict(sorted(out.items()))

    def _check_root_required(self, out: dict) -> None:
        dropped = [r for r in out.get("required") or [] if r not in self.op.fields]
        if dropped:
            raise ValueError(f"required body fields not listed: {dropped}")

    def _inject_values(self, built: Any, path: str) -> Any:
        """A scalar ALLOWED value: the node at `path` becomes anyOf [node, const]. A variant value is `_variants`'."""
        extras = [a for a in self._allowed.get(path, []) if a.value is not None and path not in self.op.variants]
        for a in extras:
            schema = {**built, "components": {"schemas": self.components}} if isinstance(built, dict) else built
            if jsonschema.Draft202012Validator(schema).is_valid(a.value):
                raise ValueError(f"stale ALLOWED at {path}: the source now accepts {a.value!r}; remove the entry")
        if not extras:
            return built
        return {"anyOf": [built, *({"const": a.value, "x-alek-extension": _note(a)} for a in extras)]}


def _note(a: Allowed) -> str:
    return f"{a.reason} ({a.date})"


def _check_allowed_kinds(op: Operation) -> None:
    for a in op.allowed:
        if not a.reason.strip() or not a.date:
            raise ValueError(f"ALLOWED at {a.path!r} without a reason or a date")
        if (a.value is None) == (a.field is None):
            raise ValueError(f"ALLOWED at {a.path!r}: exactly one of value / field")
        if a.path.startswith("header:") and a.value is None:
            raise ValueError(f"ALLOWED at {a.path!r}: a header allows values only")


def _header_parameter(name: str, literals: list[str], op: Operation) -> dict:
    """A comma-separated header (style simple) whose items are the SDK's literals plus the ALLOWED values."""
    items: dict[str, Any] = {"type": "string", "enum": sorted(set(literals))}
    extras = [a for a in op.allowed if a.path == f"header:{name}"]
    for a in extras:
        if a.value in literals:
            raise ValueError(f"stale ALLOWED at {a.path}: the source now has {a.value!r}; remove the entry")
    if extras:
        items = {"anyOf": [items, *({"const": a.value, "x-alek-extension": _note(a)} for a in extras)]}
    return {"name": name, "in": "header", "required": False, "style": "simple", "explode": False,
            "schema": {"type": "array", "items": items}}


def _reachable(paths: dict, components: dict) -> dict:
    """The components the operations reach (a collapsed union can leave one behind)."""
    keep: set[str] = set()
    todo: set[str] = set()
    _collect_refs(paths, todo)
    while todo:
        name = todo.pop().rsplit("/", 1)[-1]
        if name not in keep:
            keep.add(name)
            _collect_refs(components[name], todo)
    return {name: schema for name, schema in components.items() if name in keep}


def _collect_refs(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            out.add(node["$ref"])
        for value in node.values():
            _collect_refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_refs(value, out)


def build_subset(spec: dict, provider: Provider, *, source: str, version: str, retrieved: str,
                 transforms: tuple[str, ...] = _TRANSFORMS) -> dict:
    """The subset document of `provider` from `spec` (an OpenAPI document; an SDK source is converted to one first)."""
    paths: dict[str, Any] = {}
    components: dict[str, Any] = {}
    for op in provider.operations:
        _check_allowed_kinds(op)
        item = (spec.get("paths") or {}).get(op.path) or {}
        source_op = item.get(op.method)
        if source_op is None:
            raise ValueError(f"operation {op.method} {op.path} absent from the source")
        media = ((source_op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
        if "schema" not in media:
            raise ValueError(f"{op.method} {op.path}: no application/json request body")
        builder = _Builder(spec, op)
        body = builder.body(media["schema"])
        for name, schema in builder.components.items():
            if name in components and _canon(components[name]) != _canon(schema):
                raise ValueError(f"component {name} differs between operations")
            components[name] = schema
        built_op: dict[str, Any] = {
            "requestBody": {"required": True, "content": {"application/json": {"schema": body}}},
        }
        if source_op.get("operationId"):
            built_op["operationId"] = source_op["operationId"]
        params = [p for p in source_op.get("parameters") or [] if p.get("in") == "header" and p["name"] in op.headers]
        if params:
            built_op["parameters"] = params
        paths.setdefault(op.path, {})[op.method] = built_op
    components = _reachable(paths, components)
    servers = spec.get("servers") or ([{"url": provider.server}] if provider.server else [])
    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": f"{provider.name} request subset", "version": str(version)},
        "x-source": source, "x-retrieved": retrieved, "x-transforms": list(transforms),
        "paths": paths, "components": {"schemas": dict(sorted(components.items()))},
    }
    if servers:
        doc["servers"] = [{"url": s["url"]} for s in servers]
    return doc


def render(doc: dict) -> str:
    """Deterministic YAML: sorted keys, block style."""
    return yaml.safe_dump(doc, sort_keys=True, allow_unicode=True, default_flow_style=False, width=120)


# ------------------------------------------------------------------------------------------------------ SDK source
def _import(dotted: str) -> Any:
    module, _, name = dotted.partition(":")
    return getattr(importlib.import_module(module), name)


def _literal_values(tp: Any) -> list[str]:
    origin = typing.get_origin(tp)
    if origin is typing.Literal:
        return [v for v in typing.get_args(tp) if isinstance(v, str)]
    out: list[str] = []
    for arg in typing.get_args(tp):
        out += _literal_values(arg)
    return out


def _walk_types(tp: Any, typeddicts: dict[str, type], models: set[str]) -> None:
    """Every TypedDict reachable from `tp` (by name), and every pydantic model a union offers next to them."""
    if typing_extensions.is_typeddict(tp):  # the SDK's TypedDicts are typing_extensions'
        if tp.__name__ in typeddicts:
            return
        typeddicts[tp.__name__] = tp
        for hint in typing.get_type_hints(tp, include_extras=True).values():
            _walk_types(hint, typeddicts, models)
        return
    if isinstance(tp, type) and issubclass(tp, pydantic.BaseModel):
        models.add(tp.__name__)
        return
    for arg in typing.get_args(tp):
        _walk_types(arg, typeddicts, models)


def _aliases(td: type) -> dict[str, str]:
    out = {}
    for name, hint in typing.get_type_hints(td, include_extras=True).items():
        for meta in getattr(hint, "__metadata__", ()):
            alias = getattr(meta, "alias", None)
            if alias:
                out[name] = alias
    return out


def sdk_document(op: Operation, version: str) -> dict:
    """An OpenAPI document for one SDK operation: its request TypedDict's pydantic schema, header fields dropped,
    response-model union members dropped, header parameters from the headers' Literal types."""
    root = _import(op.sdk_body)
    typeddicts: dict[str, type] = {}
    models: set[str] = set()
    _walk_types(root, typeddicts, models)
    schema = pydantic.TypeAdapter(root).json_schema(ref_template=_REF_PREFIX + "{model}")
    defs = schema.pop("$defs", {})
    root_name = root.__name__
    defs[root_name] = schema
    for td_name, td in typeddicts.items():
        for fname, alias in _aliases(td).items():
            if op.header_aliases.get(fname) != alias:
                raise ValueError(f"{td_name}.{fname} has an alias {alias!r} the table does not know: the SDK "
                                 f"renames it on the wire; add it to header_aliases if it is a header, else extend "
                                 f"the extractor")
            target = defs.get(td_name)
            if target is None:
                raise ValueError(f"no schema for {td_name}")
            (target.get("properties") or {}).pop(fname, None)
            if fname in (target.get("required") or []):
                target["required"] = [r for r in target["required"] if r != fname]

    def strip_models(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: strip_models(v) for k, v in node.items()}
            for key in ("anyOf", "oneOf"):
                if key in node:
                    node[key] = [m for m in node[key] if not (
                        isinstance(m, dict) and m.get("$ref", "").rsplit("/", 1)[-1] in models)]
            return node
        if isinstance(node, list):
            return [strip_models(v) for v in node]
        return node

    defs = {name: strip_models(s) for name, s in defs.items() if name not in models}
    params = [_header_parameter(name, _literal_values(_import(tp)), op) for name, tp in sorted(op.headers.items())]
    source_op: dict[str, Any] = {
        "requestBody": {"content": {"application/json": {"schema": {"$ref": _REF_PREFIX + root_name}}}},
    }
    if params:
        source_op["parameters"] = params
    return {"openapi": "3.1.0", "info": {"version": version},
            "paths": {op.path: {op.method: source_op}}, "components": {"schemas": defs}}


def sdk_subset(provider: Provider, retrieved: str) -> dict:
    """The subset of an SDK-sourced provider, from the installed package (offline)."""
    version = importlib.import_module(provider.sdk).__version__
    docs = [sdk_document(op, version) for op in provider.operations]
    merged: dict[str, Any] = {"paths": {}, "components": {"schemas": {}}}
    for doc in docs:
        for path, item in doc["paths"].items():
            merged["paths"].setdefault(path, {}).update(item)
        merged["components"]["schemas"].update(doc["components"]["schemas"])
    return build_subset(merged, provider, source=f"{provider.sdk}-sdk-python@{version}", version=version,
                        retrieved=retrieved, transforms=_TRANSFORMS + _SDK_TRANSFORMS)


# --------------------------------------------------------------------------------------------------- OpenAPI source
def spec_url(provider: Provider, ref: str) -> str:
    return f"https://raw.githubusercontent.com/{provider.repo}/{ref}/{provider.spec_file}"


def head_commit(repo: str) -> str:
    """The commit the default branch points at (pins the source)."""
    out = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}", "HEAD"],
                         check=True, capture_output=True, text=True, timeout=60).stdout
    sha = out.split()[0] if out.split() else ""
    if len(sha) != 40:
        raise RuntimeError(f"no HEAD commit for {repo}: {out!r}")
    return sha


def openapi_subset(provider: Provider, ref: str, retrieved: str, fetch: Callable[[str], bytes] = fetch_url) -> dict:
    url = spec_url(provider, ref)
    spec = parse(fetch(url))
    version = (spec.get("info") or {}).get("version", "")
    return build_subset(spec, provider, source=url, version=version, retrieved=retrieved)


# ------------------------------------------------------------------------------------------------------------- CLI
def write(provider_name: str, doc: dict, out_dir: Path = CONTRACTS_DIR) -> Path:
    text = render(doc)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"{provider_name}: subset is {len(text.encode())} bytes, over the {MAX_BYTES} cap")
    path = out_dir / provider_name / "openapi.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", required=True, choices=sorted(PROVIDERS))
    parser.add_argument("--ref", default="", help="OpenAPI source: the commit to pin (default: the branch head)")
    args = parser.parse_args(argv)
    provider = PROVIDERS[args.provider]
    today = _dt.date.today().isoformat()
    if provider.sdk:
        if args.ref:
            raise SystemExit("--ref applies to an OpenAPI source; an SDK source is pinned by the installed version")
        doc = sdk_subset(provider, today)
    else:
        doc = openapi_subset(provider, args.ref or head_commit(provider.repo), today)
    print(f"wrote {write(provider.name, doc)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
