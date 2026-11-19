"""Validate a request we send, and a response we receive, against a provider's contract,
`contracts/providers/<provider>/openapi.yaml`.

TEST_COVERAGE_SYSTEMIC_RFC §4a, P17. The contracts are subsets extracted by `scripts/contracts/extract_subset.py`
(never edited by hand). Body schemas are JSON Schema Draft 2020-12 (OpenAPI 3.1), validated with `jsonschema`; one
validator per operation is compiled once per process (a response's on first use). In a request, a field the schema
marks `deprecated` fails unless the contract allows it (`x-alek-extension`, from the extractor's ALLOWED table); in a
response it is accepted. `assert_response` checks one body (one event's data for a stream); `assert_stream` a whole
server-sent-event stream.
JSON Schema `format` keywords (date-time, uri, ...) are not enforced: no FormatChecker is passed.

A failure lists every violation, one per line: `<where>: unknown field | wrong type | missing required | value not
allowed | deprecated`. `<where>` is a JSON path into the body (`$.input[1].content[0].type`) or `header <name>`. Where
a union's variants carry a `type` const, the message comes from the variant the request's `type` names, not from the
whole union.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse

import jsonschema
import yaml
from jsonschema.exceptions import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts" / "providers"
EXTENSION = "x-alek-extension"


def assert_request(
    provider: str,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: Any,
    *,
    contracts_dir: Path = CONTRACTS_DIR,
) -> None:
    """Fail (AssertionError, every violation on its own line) unless the request conforms to the provider's contract.

    `path` is the operation's path as the contract writes it, or the URL path the client sent (the base path of the
    contract's server, e.g. `/v1`, is stripped). Only the headers the operation declares are checked: the SDK adds
    its own (auth, user agent, retries). `contracts_dir` is for the extractor's own tests."""
    op = _operation(contracts_dir / provider / "openapi.yaml", method.lower(), path)
    problems = sorted(set(op.problems(headers, body)))
    if problems:
        raise AssertionError(f"{provider} {method.upper()} {path} breaks the contract:\n" + "\n".join(problems))


def assert_response(
    provider: str,
    method: str,
    path: str,
    status: int | str,
    body: Any,
    *,
    media: str = "application/json",
    contracts_dir: Path = CONTRACTS_DIR,
) -> None:
    """Fail (AssertionError, every violation on its own line) unless a response body conforms to the provider's
    contract for that operation, status and media type.

    For `text/event-stream` the body is ONE event's data JSON (see `assert_stream` for a whole stream). A field the
    source marks deprecated is accepted in a response (the provider may still send it); a top-level field the contract
    keeps as `x-alek-unread` accepts any value."""
    op = _operation(contracts_dir / provider / "openapi.yaml", method.lower(), path)
    problems = sorted(set(op.response_problems(str(status), media, body, "$")))
    if problems:
        raise AssertionError(
            f"{provider} {method.upper()} {path} {status} {media} breaks the contract:\n" + "\n".join(problems))


def assert_stream(
    provider: str,
    method: str,
    path: str,
    status: int | str,
    events: list[Mapping[str, Any]],
    *,
    contracts_dir: Path = CONTRACTS_DIR,
) -> None:
    """Fail unless every event of a server-sent-event stream conforms: `events` is `[{"event": <name>, "data":
    <JSON>}]` in wire order. Each violation is prefixed `event[i]`; an event whose name differs from its data's
    `type` is a violation too (the SDK dispatches on the name)."""
    op = _operation(contracts_dir / provider / "openapi.yaml", method.lower(), path)
    problems: list[str] = []
    for i, event in enumerate(events):
        data = event.get("data")
        name = event.get("event")
        if isinstance(data, dict) and name is not None and data.get("type") != name:
            problems.append(f"event[{i}]: event name {name!r} differs from data.type {data.get('type')!r}")
        problems += sorted(set(op.response_problems(str(status), "text/event-stream", data, f"event[{i}] $")))
    if problems:
        raise AssertionError(
            f"{provider} {method.upper()} {path} {status} text/event-stream breaks the contract:\n" + "\n".join(problems))


# ------------------------------------------------------------------------------------------------------- loading
def _deprecated(validator: Any, value: Any, instance: Any, schema: Mapping[str, Any]) -> Iterator[ValidationError]:
    if value is True and EXTENSION not in schema:
        yield ValidationError("deprecated")


_Validator = jsonschema.validators.extend(jsonschema.Draft202012Validator, {"deprecated": _deprecated})


@functools.lru_cache(maxsize=None)
def _document(contract: Path) -> dict:
    if not contract.exists():
        raise AssertionError(f"no contract at {contract}")
    return yaml.safe_load(contract.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=None)
def _operation(contract: Path, method: str, path: str) -> "_Operation":
    doc = _document(contract)
    paths = doc.get("paths") or {}
    candidates = [path] + [path[len(base):] for base in _base_paths(doc) if base and path.startswith(base)]
    for candidate in candidates:
        op = (paths.get(candidate) or {}).get(method)
        if op is not None:
            return _Operation(doc, op)
    raise AssertionError(f"{method.upper()} {path} is not in {contract} (operations: {sorted(paths)})")


def _base_paths(doc: dict) -> list[str]:
    return [urlparse(s.get("url", "")).path.rstrip("/") for s in doc.get("servers") or []]


class _Operation:
    """One operation's compiled validators: the body and each declared header; each response (status, media type)
    compiled on first use."""

    def __init__(self, doc: dict, op: dict):
        self.schemas = (doc.get("components") or {}).get("schemas") or {}
        self.op = op
        body = op["requestBody"]["content"]["application/json"]["schema"]
        self.body = self._compile(body)
        self.headers = [(p["name"], bool(p.get("required")), p["schema"], self._compile(p["schema"]))
                        for p in op.get("parameters") or [] if p.get("in") == "header"]
        self._responses: dict[tuple[str, str], Any] = {}

    def _compile(self, schema: dict, validator: Any = _Validator) -> Any:
        root = {**schema, "components": {"schemas": self.schemas}}
        validator.check_schema(root)
        return validator(root)

    def response_problems(self, status: str, media: str, body: Any, root: str) -> Iterator[str]:
        key = (status, media)
        if key not in self._responses:
            responses = self.op.get("responses") or {}
            content = (responses.get(status) or {}).get("content") or {}
            if media not in content:
                known = sorted((s, m) for s, r in responses.items() for m in r.get("content") or {})
                raise AssertionError(f"no {status} {media} response in the contract (it has {known})")
            # Responses: a deprecated field is the provider's to send, so the plain Draft 2020-12 validator.
            self._responses[key] = self._compile(content[media]["schema"], jsonschema.Draft202012Validator)
        for error in self._responses[key].iter_errors(body):
            yield from self._explain(error, root)

    def problems(self, headers: Mapping[str, str], body: Any) -> Iterator[str]:
        for error in self.body.iter_errors(body):
            yield from self._explain(error, "$")
        sent = {k.lower(): v for k, v in headers.items()}
        for name, required, schema, validator in self.headers:
            value = sent.get(name.lower())
            if value is None:
                if required:
                    yield f"header {name}: missing required"
                continue
            instance: Any = [v.strip() for v in value.split(",") if v.strip()] if schema.get("type") == "array" \
                else value
            for error in validator.iter_errors(instance):
                yield from self._explain(error, f"header {name}")

    # -- messages
    def _explain(self, error: ValidationError, root: str) -> Iterator[str]:
        where = root + error.json_path[1:]
        kind, value, instance = error.validator, error.validator_value, error.instance
        if kind in ("anyOf", "oneOf"):
            yield from self._explain_union(error, root, where)
        elif kind == "additionalProperties":
            known = set((error.schema.get("properties") or {}))
            for extra in sorted(set(instance) - known):
                yield f"{where}.{extra}: unknown field"
        elif kind == "required":
            for name in value:
                if isinstance(instance, dict) and name not in instance:
                    yield f"{where}.{name}: missing required"
        elif kind == "type":
            yield f"{where}: wrong type (expected {value})"
        elif kind in ("enum", "const"):
            yield f"{where}: value not allowed ({instance!r})"
        elif kind == "deprecated":
            yield f"{where}: deprecated"
        else:
            yield f"{where}: value not allowed ({kind} {value!r})"

    def _explain_union(self, error: ValidationError, root: str, where: str) -> Iterator[str]:
        branches = [self._resolve(b) for b in error.validator_value]
        by_branch: dict[int, list[ValidationError]] = {}
        for sub in error.context or ():
            by_branch.setdefault(sub.relative_schema_path[0], []).append(sub)
        instance = error.instance
        chosen = _candidates(instance, branches)
        if not chosen:
            if isinstance(instance, dict) and isinstance(instance.get("type"), str) and any(map(_tags, branches)):
                yield f"{where}.type: value not allowed ({instance['type']!r})"
            else:
                yield f"{where}: wrong type (expected one of {sorted(set().union(*map(_json_types, branches)))})"
            return
        best = min(chosen, key=lambda i: (len(by_branch.get(i, ())), i))
        for sub in by_branch.get(best, ()):
            yield from self._explain(sub, root)

    def _resolve(self, schema: Any) -> Any:
        while isinstance(schema, dict) and "$ref" in schema:
            schema = self.schemas[schema["$ref"].rsplit("/", 1)[-1]]
        return schema


# -------------------------------------------------------------------------------------------- union discrimination
_JSON_TYPES = {dict: "object", list: "array", str: "string", bool: "boolean", type(None): "null"}


def _json_type(instance: Any) -> set[str]:
    if isinstance(instance, bool):
        return {"boolean"}
    if isinstance(instance, int):
        return {"integer", "number"}
    if isinstance(instance, float):
        return {"number"}
    return {_JSON_TYPES.get(type(instance), "unknown")}


def _json_types(schema: Any) -> set[str]:
    """The JSON types a (resolved) branch can accept; empty when it does not say."""
    if not isinstance(schema, dict):
        return set()
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return set(declared)
    if "properties" in schema:
        return {"object"}
    if "const" in schema or "enum" in schema:
        values = [schema["const"]] if "const" in schema else schema["enum"]
        return set().union(*(_json_type(v) for v in values)) if values else set()
    return set()


def _tags(schema: Any) -> set[str]:
    """The `type` const/enum values of an object branch (its discriminator), or empty."""
    if not isinstance(schema, dict):
        return set()
    tag = (schema.get("properties") or {}).get("type")
    if not isinstance(tag, dict):
        return set()
    found = {tag["const"]} if "const" in tag else set(tag.get("enum") or [])
    for member in tag.get("anyOf") or []:
        found |= {member["const"]} if "const" in member else set(member.get("enum") or [])
    return {t for t in found if isinstance(t, str)}


def _candidates(instance: Any, branches: list[Any]) -> list[int]:
    """The branches the instance means: same JSON type; for an object, the variants its `type` names, or (no `type`
    sent) the variants that do not require one."""
    kinds = _json_type(instance)
    typed = [i for i, b in enumerate(branches) if not _json_types(b) or _json_types(b) & kinds]
    if not isinstance(instance, dict):
        return typed
    tag = instance.get("type")
    if isinstance(tag, str):
        hit = [i for i in typed if tag in _tags(branches[i])]
        if hit or any(_tags(branches[i]) for i in typed):
            return hit
        return typed
    return [i for i in typed if "type" not in (branches[i].get("required") or [])]
