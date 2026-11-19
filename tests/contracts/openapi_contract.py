"""Validate a request we send, and a response we receive, against a provider's contract,
`contracts/providers/<provider>/openapi.yaml`.

TEST_COVERAGE_SYSTEMIC_RFC §4a, P17. The contracts are subsets extracted by `scripts/contracts/extract_subset.py`
(never edited by hand). Body schemas are JSON Schema Draft 2020-12 (OpenAPI 3.1), validated with `jsonschema`; one
validator per operation is compiled once per process (a response's on first use). In a request, a field the schema
marks `deprecated` fails unless the contract allows it (`x-alek-extension`, from the extractor's ALLOWED table); in a
response it is accepted. `assert_response` checks one body (one event's data for a stream); `assert_stream` a whole
server-sent-event stream.
JSON Schema `format` keywords (date-time, uri, ...) are not enforced: no FormatChecker is passed.

Paths: a request's path is the decoded URL path the client sent (`yarl.URL.path`, `httpx.URL.path`), never with a query
or fragment. When the contract's server has a base path (`/v1`), the path must start with it and it is stripped; a path
without it is accepted only for a server with an empty base path. The operation is found by its literal path, else by a
templated one matched segment by segment (`/messages/{id}`; a literal match wins); the matched segments are the path
parameters.

Query: the caller always says what was sent (`{}` for nothing). Values arrive as strings and are read per the
parameter's schema (integer and number in their canonical ASCII form, boolean; an array is a repeated parameter, or one
comma-separated value when the parameter says `explode: false`) before validation. A path or query parameter the
contract marks `deprecated` fails when sent, unless it carries the extension.

Aliases: a contract with `x-alek-aliases` (Microsoft Graph) rewrites the request path before the lookup, in order, the
first matching rule only: `pattern` is written like a contract path (`{name}` is a segment's text or part of one, a
trailing `/**` the rest of the path), and the path is looked up as `target` with the captured values put in (a `{name}`
the pattern does not capture becomes `(alias)`; the rest of the path follows). Such a contract is matched on the RAW
path as well (`raw_path=`, still percent-encoded as sent, `yarl.URL.raw_path`; the caller must pass it and it must
decode to `path`): a captured value never holds a bare `:` (colon addressing would otherwise read `a:b:` as an item),
an encoded `/` (`%2F`) stays inside its segment, and every path parameter is decoded after the match and validated as
usual. A contract without aliases is matched on the decoded path, as before, and refuses `raw_path`.

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
    query: Mapping[str, Sequence[str]],
    raw_path: str | None = None,
    contracts_dir: Path = CONTRACTS_DIR,
) -> None:
    """Fail (AssertionError, every violation on its own line) unless the request conforms to the provider's contract.

    `path` is the decoded URL path the client sent, the server's base path included (see the module docstring). Only the headers the operation declares are checked: the SDK adds
    its own (auth, user agent, retries). `query` maps each query parameter sent to its values in order, as strings;
    `{}` when none was sent. `body` is None for a request without one. `raw_path` is the same path as sent, still
    percent-encoded: required by a contract with aliases, refused by any other. `contracts_dir` is for the tests of this
    module and of the extractor."""
    if query is None:
        raise TypeError("query: pass {} when the request carries no query")
    contract = contracts_dir / provider / "openapi.yaml"
    template, path_params = _match(contract, method.lower(), _plain_path(path), _raw(contract, path, raw_path))
    op = _operation(contract, method.lower(), template)
    problems = sorted(set(op.problems(headers, body, dict(path_params), query)))
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
    raw_path: str | None = None,
    contracts_dir: Path = CONTRACTS_DIR,
) -> None:
    """Fail (AssertionError, every violation on its own line) unless a response body conforms to the provider's
    contract for that operation, status and media type.

    For `text/event-stream` the body is ONE event's data JSON (see `assert_stream` for a whole stream). A field the
    source marks deprecated is accepted in a response (the provider may still send it); a top-level field the contract
    keeps as `x-alek-unread` accepts any value. `path` and `raw_path` are those of the request the response answers,
    resolved exactly as `assert_request` resolves them (the decoded URL path, the server's base path included; the
    raw path required by a contract with aliases, refused by any other)."""
    op = _response_operation(contracts_dir / provider / "openapi.yaml", method.lower(), path, raw_path)
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
    raw_path: str | None = None,
    contracts_dir: Path = CONTRACTS_DIR,
) -> None:
    """Fail unless every event of a server-sent-event stream conforms: `events` is `[{"event": <name>, "data":
    <JSON>}]` in wire order. Each violation is prefixed `event[i]`; an event whose name differs from its data's
    `type` is a violation too (the SDK dispatches on the name). `path` and `raw_path` as for `assert_response`."""
    op = _response_operation(contracts_dir / provider / "openapi.yaml", method.lower(), path, raw_path)
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


def operation_path(provider: str, method: str, path: str, *, raw_path: str | None = None,
                   contracts_dir: Path = CONTRACTS_DIR) -> str:
    """The contract path (`paths` key) a request's method and decoded URL path resolve to; AssertionError when none
    does. `raw_path` as for `assert_request`."""
    contract = contracts_dir / provider / "openapi.yaml"
    return _match(contract, method.lower(), _plain_path(path), _raw(contract, path, raw_path))[0]


def _raw(contract: Path, path: str, raw_path: str | None) -> str | None:
    """The raw path when the contract has aliases (required, and it must decode to `path`); None otherwise."""
    if not _document(contract).get("x-alek-aliases"):
        if raw_path is not None:
            raise TypeError("raw_path: only a contract with aliases is matched on the raw path")
        return None
    if raw_path is None:
        raise TypeError("raw_path: this contract has aliases; pass the URL path as sent, still percent-encoded")
    if unquote(_plain_path(raw_path)) != path:
        raise ValueError(f"raw_path {raw_path!r} does not decode to path {path!r}")
    return raw_path


def _plain_path(path: str) -> str:
    if "?" in path or "#" in path:
        raise ValueError(f"{path!r}: pass the URL path alone; the query goes in `query`")
    return path


def _response_operation(contract: Path, method: str, path: str, raw_path: str | None) -> "_Operation":
    """The operation a response answers, resolved from its request's paths exactly as `assert_request` does."""
    template, _ = _match(contract, method, _plain_path(path), _raw(contract, path, raw_path))
    return _operation(contract, method, template)


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
def _match(contract: Path, method: str, path: str, raw: str | None = None) -> tuple[str, tuple[tuple[str, str], ...]]:
    """The `paths` key the request resolves to and its path parameters. A literal path wins; otherwise the templated
    path with the most literal segments (a tie between two templates is ambiguous and fails). With `raw` (a contract
    with aliases) the match runs on the raw path and the parameter values are decoded after it."""
    doc = _document(contract)
    paths = doc.get("paths") or {}
    stripped = _stripped(doc, path if raw is None else raw)
    candidates = [_aliased(doc, candidate) for candidate in stripped]
    for candidate in candidates:
        if method in (paths.get(candidate) or {}):
            return candidate, ()
    found: dict[str, tuple[int, tuple[tuple[str, str], ...]]] = {}
    for candidate in candidates:
        for template, item in paths.items():
            if "{" in template and method in (item or {}) and template not in found:
                params = _template_params(template, candidate)
                if params is not None and raw is not None:
                    params = tuple((name, unquote(value)) for name, value in params)
                if params is not None:
                    found[template] = (sum("{" not in seg for seg in template.split("/")), params)
    if not found:
        aliased = [c for c, before in zip(candidates, stripped) if c != before]
        via = f" (aliased to {aliased[-1]})" if aliased else ""
        raise AssertionError(f"{method.upper()} {path}{via} is not in {contract} (operations: {sorted(paths)})")
    best = max(literal for literal, _ in found.values())
    winners = sorted(t for t, (literal, _) in found.items() if literal == best)
    if len(winners) > 1:
        raise AssertionError(f"{method.upper()} {path} matches several operations of {contract}: {winners}")
    return winners[0], found[winners[0]][1]


def _stripped(doc: dict, path: str) -> list[str]:
    """The path without the server's base path (one candidate per server whose base path it starts with)."""
    bases = _base_paths(doc) or [""]
    return [path[len(base):] if base else path for base in bases if not base or path.startswith(base + "/")]


def _template_params(template: str, path: str) -> tuple[tuple[str, str], ...] | None:
    """The `{name}` values of `path` (already decoded) under `template`, segment by segment (a value never spans a
    `/`); None: no match."""
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
        params += list(zip(parts[1::2], hit.groups()))
    return tuple(params)


_ALIAS_FILL = "(alias)"


def applied_alias(provider: str, path: str, *, raw_path: str | None, contracts_dir: Path = CONTRACTS_DIR) -> str | None:
    """The pattern of the `x-alek-aliases` rule that rewrites a request's URL path (None: no rule applies); the paths
    as for `assert_request`."""
    contract = contracts_dir / provider / "openapi.yaml"
    doc = _document(contract)
    raw = _raw(contract, _plain_path(path), raw_path)
    for candidate in _stripped(doc, raw if raw is not None else path):
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
    """The `{name}` values of the raw path segments under `template`, still encoded; a value never holds a bare `:`
    (a bare colon is OData's colon addressing; OneDrive refuses ':' in a name). None: no match."""
    want = template.split("/")
    if len(want) != len(segments):
        return None
    out: dict[str, str] = {}
    for pattern, segment in zip(want, segments):
        parts = re.split(r"\{([^}]+)\}", pattern)
        hit = re.fullmatch("".join("([^:]+?)" if i % 2 else re.escape(p) for i, p in enumerate(parts)), segment)
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
    """One operation's compiled validators: the body (None: the operation takes none) and each declared parameter;
    each response (status, media type) compiled on first use."""

    def __init__(self, doc: dict, op: dict):
        self.schemas = (doc.get("components") or {}).get("schemas") or {}
        self.op = op
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
            yield from _deprecated_parameter(param, f"path {name}")
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
            yield from _deprecated_parameter(param, f"query {name}")
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


def _deprecated_parameter(param: Mapping[str, Any], where: str) -> Iterator[str]:
    """A sent parameter the contract marks deprecated (OpenAPI puts the mark on the Parameter Object), unless allowed."""
    if param.get("deprecated") is True and EXTENSION not in param:
        yield f"{where}: deprecated"


_INTEGER = re.compile(r"-?(0|[1-9][0-9]*)")                       # as a client writes an int: no sign, no zero padding
_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")


def _scalar(text: str, schema: Mapping[str, Any]) -> Any:
    """A path or query value (a string on the wire) as the schema's type, when it reads as one in canonical ASCII
    form; else the string, which then fails as a wrong type."""
    kind = schema.get("type")
    if kind == "integer" and _INTEGER.fullmatch(text):
        return int(text)
    if kind == "number" and _NUMBER.fullmatch(text):
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
