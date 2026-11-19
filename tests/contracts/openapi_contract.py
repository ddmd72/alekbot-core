"""Validate a request we send against a provider's request contract, `contracts/providers/<provider>/openapi.yaml`.

TEST_COVERAGE_SYSTEMIC_RFC §4a, P17. The contracts are subsets extracted by `scripts/contracts/extract_subset.py`
(never edited by hand). Body schemas are JSON Schema Draft 2020-12 (OpenAPI 3.1), validated with `jsonschema`; one
validator per operation is compiled once per process. A field the schema marks `deprecated` fails unless the contract
allows it (`x-alek-extension`, from the extractor's ALLOWED table).
JSON Schema `format` keywords (date-time, uri, ...) are not enforced: no FormatChecker is passed.

Path and query parameters: the operation is found by its literal path, else by a templated one matched segment by
segment (`/messages/{id}`; a literal match wins); the matched segments are the path parameters. Query values arrive as
strings and are read per the parameter's schema (integer, number, boolean; an array is a repeated parameter, or one
comma-separated value when the parameter says `explode: false`) before validation.

Aliases: a contract with `x-alek-aliases` (Microsoft Graph) rewrites the request path before the lookup, in order, the
first matching rule only: `pattern` is written like a contract path (`{name}` is a segment's text or part of one, a
trailing `/**` the rest of the path), and the path is looked up as `target` with the captured values put in (a `{name}`
the pattern does not capture becomes `(alias)`; the rest of the path follows). The template match then validates the
path parameters as usual. A contract without aliases is matched as before.

Request bodies: an operation whose body is not JSON (an upload's `application/octet-stream`) accepts bytes or text
unvalidated and fails a JSON body (a dict or a list).

A failure lists every violation, one per line: `<where>: unknown field | unknown query parameter | wrong type | missing
required | value not allowed | deprecated`. `<where>` is a JSON path into the body (`$.input[1].content[0].type`),
`header <name>`, `path <name>` or `query <name>`. Where a union's variants carry a `type` const, the message comes from
the variant the request's `type` names, not from the whole union.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import unquote, urlparse

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
    query: Mapping[str, Sequence[str]] | None = None,
    contracts_dir: Path = CONTRACTS_DIR,
) -> None:
    """Fail (AssertionError, every violation on its own line) unless the request conforms to the provider's contract.

    `path` is the operation's path as the contract writes it, or the URL path the client sent (the base path of the
    contract's server, e.g. `/v1`, is stripped; a templated contract path matches it segment by segment). Only the
    headers the operation declares are checked: the SDK adds its own (auth, user agent, retries). `query` maps each
    query parameter sent to its values in order, as strings (`None`: no query was sent). `body` is None for a request
    without one. `contracts_dir` is for the extractor's own tests."""
    contract = contracts_dir / provider / "openapi.yaml"
    template, path_params = _match(contract, method.lower(), path)
    op = _operation(contract, method.lower(), template)
    problems = sorted(set(op.problems(headers, body, dict(path_params), query or {})))
    if problems:
        raise AssertionError(f"{provider} {method.upper()} {path} breaks the contract:\n" + "\n".join(problems))


def operation_path(provider: str, method: str, path: str, *, contracts_dir: Path = CONTRACTS_DIR) -> str:
    """The contract path (`paths` key) a request's method and URL path resolve to; AssertionError when none does."""
    return _match(contracts_dir / provider / "openapi.yaml", method.lower(), path)[0]


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
def _match(contract: Path, method: str, path: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    """The `paths` key the request resolves to and its path parameters. A literal path wins; otherwise the templated
    path with the most literal segments (a tie between two templates is ambiguous and fails)."""
    doc = _document(contract)
    paths = doc.get("paths") or {}
    candidates = [path] + [path[len(base):] for base in _base_paths(doc) if base and path.startswith(base)]
    candidates = [_aliased(doc, candidate) for candidate in candidates]
    for candidate in candidates:
        if method in (paths.get(candidate) or {}):
            return candidate, ()
    found: dict[str, tuple[int, tuple[tuple[str, str], ...]]] = {}
    for candidate in candidates:
        for template, item in paths.items():
            if "{" in template and method in (item or {}) and template not in found:
                params = _template_params(template, candidate)
                if params is not None:
                    found[template] = (sum("{" not in seg for seg in template.split("/")), params)
    if not found:
        aliased = [c for c in candidates if c not in (path, *(path[len(b):] for b in _base_paths(doc)))]
        via = f" (aliased to {aliased[-1]})" if aliased else ""
        raise AssertionError(f"{method.upper()} {path}{via} is not in {contract} (operations: {sorted(paths)})")
    best = max(literal for literal, _ in found.values())
    winners = sorted(t for t, (literal, _) in found.items() if literal == best)
    if len(winners) > 1:
        raise AssertionError(f"{method.upper()} {path} matches several operations of {contract}: {winners}")
    return winners[0], found[winners[0]][1]


def _template_params(template: str, path: str) -> tuple[tuple[str, str], ...] | None:
    """The `{name}` values of `path` under `template`, segment by segment (a value never spans a `/`); None: no match."""
    want, got = template.split("/"), path.split("/")
    if len(want) != len(got):
        return None
    params: list[tuple[str, str]] = []
    for pattern, segment in zip(want, got):
        parts = re.split(r"\{([^}]+)\}", pattern)   # "a{x}b" -> ["a", "x", "b"]: names at the odd indexes
        if len(parts) == 1:
            if pattern != segment:
                return None
            continue
        hit = re.fullmatch("".join("(.+?)" if i % 2 else re.escape(p) for i, p in enumerate(parts)), segment)
        if hit is None or not all(hit.groups()):
            return None
        params += [(name, unquote(value)) for name, value in zip(parts[1::2], hit.groups())]
    return tuple(params)


_ALIAS_FILL = "(alias)"


def applied_alias(provider: str, path: str, *, contracts_dir: Path = CONTRACTS_DIR) -> str | None:
    """The pattern of the `x-alek-aliases` rule that rewrites a request's URL path (None: no rule applies)."""
    doc = _document(contracts_dir / provider / "openapi.yaml")
    for candidate in [path] + [path[len(base):] for base in _base_paths(doc) if base and path.startswith(base)]:
        rule, _ = _alias_rule(doc, candidate)
        if rule is not None:
            return rule["pattern"]
    return None


def _aliased(doc: dict, path: str) -> str:
    """`path` rewritten by the first `x-alek-aliases` rule whose pattern matches it; unchanged when none does."""
    return _alias_rule(doc, path)[1]


def _alias_rule(doc: dict, path: str) -> tuple[dict | None, str]:
    for rule in doc.get("x-alek-aliases") or []:
        pattern, target = rule["pattern"], rule["target"]
        segments = path.split("/")
        rest: list[str] = []
        if pattern.endswith("/**"):
            pattern, target = pattern[:-3], target[:-3]
            fixed = len(pattern.split("/"))
            if len(segments) <= fixed:
                continue
            segments, rest = segments[:fixed], segments[fixed:]
        captured = _capture(pattern, segments)
        if captured is None:
            continue
        filled = re.sub(r"\{([^}]+)\}", lambda m: captured.get(m.group(1), _ALIAS_FILL), target)
        return rule, "/".join([filled, *rest])
    return None, path


def _capture(template: str, segments: list[str]) -> dict[str, str] | None:
    """The `{name}` values of the path segments under `template`, as written (not decoded); None: no match."""
    want = template.split("/")
    if len(want) != len(segments):
        return None
    out: dict[str, str] = {}
    for pattern, segment in zip(want, segments):
        parts = re.split(r"\{([^}]+)\}", pattern)
        hit = re.fullmatch("".join("(.+?)" if i % 2 else re.escape(p) for i, p in enumerate(parts)), segment)
        if hit is None:
            return None
        out.update(zip(parts[1::2], hit.groups()))
    return out


@functools.lru_cache(maxsize=None)
def _operation(contract: Path, method: str, template: str) -> "_Operation":
    op = ((_document(contract).get("paths") or {}).get(template) or {}).get(method)
    if op is None:
        raise AssertionError(f"{method.upper()} {template} is not in {contract}")
    return _Operation(_document(contract), op)


def _base_paths(doc: dict) -> list[str]:
    return [urlparse(s.get("url", "")).path.rstrip("/") for s in doc.get("servers") or []]


class _Operation:
    """One operation's compiled validators: the body (None: the operation takes none) and each declared parameter."""

    def __init__(self, doc: dict, op: dict):
        self.schemas = (doc.get("components") or {}).get("schemas") or {}
        body = op.get("requestBody") or {}
        content = body.get("content") or {}
        self.body = self._compile(content["application/json"]["schema"]) if "application/json" in content else None
        # a body that is not JSON (an upload's bytes): its media type, never validated
        self.raw_media = next(iter(content), None) if content and self.body is None else None
        self.body_required = bool(body.get("required"))
        self.params: dict[str, list[tuple[dict, Any]]] = {"header": [], "path": [], "query": []}
        for p in op.get("parameters") or []:
            if p.get("in") in self.params:
                self.params[p["in"]].append((p, self._compile(p["schema"])))

    def _compile(self, schema: dict) -> Any:
        root = {**schema, "components": {"schemas": self.schemas}}
        _Validator.check_schema(root)
        return _Validator(root)

    def problems(self, headers: Mapping[str, str], body: Any, path_params: Mapping[str, str],
                 query: Mapping[str, Sequence[str]]) -> Iterator[str]:
        if self.body is not None:
            for error in self.body.iter_errors(body):
                yield from self._explain(error, "$")
        elif self.raw_media is not None:
            if body is None:
                if self.body_required:
                    yield f"$: missing required (a {self.raw_media} body)"
            elif not isinstance(body, (bytes, bytearray, memoryview, str)):
                yield f"$: wrong type (the operation takes a {self.raw_media} body, not JSON)"
        elif body is not None:
            yield "$: unknown field (the operation takes no request body)"
        sent = {k.lower(): v for k, v in headers.items()}
        for param, validator in self.params["header"]:
            name, schema = param["name"], param["schema"]
            value = sent.get(name.lower())
            if value is None:
                if param.get("required"):
                    yield f"header {name}: missing required"
                continue
            instance: Any = [v.strip() for v in value.split(",") if v.strip()] if schema.get("type") == "array" \
                else value
            for error in validator.iter_errors(instance):
                yield from self._explain(error, f"header {name}")
        for param, validator in self.params["path"]:
            name = param["name"]
            if name not in path_params:
                yield f"path {name}: missing required"
                continue
            yield from self._explain_all(validator, _scalar(path_params[name], param["schema"]), f"path {name}")
        known = {param["name"] for param, _ in self.params["query"]}
        for name in sorted(set(query) - known):
            yield f"query {name}: unknown query parameter"
        for param, validator in self.params["query"]:
            name, schema = param["name"], param["schema"]
            values = query.get(name)
            if isinstance(values, (str, bytes)):
                raise TypeError(f"query {name}: pass the values as a list of strings, not one string")
            if not values:
                if param.get("required"):
                    yield f"query {name}: missing required"
                continue
            if schema.get("type") == "array":
                if param.get("explode") is False:
                    values = [part for value in values for part in value.split(",")]
                instance = [_scalar(v, schema.get("items") or {}) for v in values]
            elif len(values) > 1:
                yield f"query {name}: wrong type (expected one value, got {len(values)})"
                continue
            else:
                instance = _scalar(values[0], schema)
            yield from self._explain_all(validator, instance, f"query {name}")

    def _explain_all(self, validator: Any, instance: Any, root: str) -> Iterator[str]:
        for error in validator.iter_errors(instance):
            yield from self._explain(error, root)

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


def _scalar(text: str, schema: Mapping[str, Any]) -> Any:
    """A path or query value (a string on the wire) as the schema's type, when it reads as one; else the string, which
    then fails as a wrong type."""
    kind = schema.get("type")
    if kind == "integer" and re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if kind == "number" and re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text):
        return float(text)
    if kind == "boolean" and text in ("true", "false"):
        return text == "true"
    return text


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
