"""Every request shape OpenAIAdapter builds conforms to the OpenAI request contract (P17, TEST_COVERAGE_SYSTEMIC_RFC §4a).

The REAL adapter and the REAL SDK run with the HTTP transport replaced (`httpx.MockTransport`): the request the SDK
would put on the wire (its query included) is captured and validated by `assert_request`; the sink answers 400, the adapter raises its
client error, nothing about the response is asserted. No network, no key. `openai_request_shapes.SHAPES` is the table.

Meta-tests: the closed subset rejects an unknown field; every kept field and union variant of the contract is used by a
shape (a dead keep-list entry fails); every `self.client...(` call chain of the adapter maps to an operation.
"""
from __future__ import annotations

import asyncio
import copy
import functools
import json
import re
from pathlib import Path

import httpx
import pytest
import yaml
from openai import AsyncOpenAI

from scripts.contracts.extract_subset import PROVIDERS
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.exceptions import LLMClientError
from tests.contracts.openapi_contract import assert_request
from tests.unit.contracts.openai_request_shapes import SHAPES

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = REPO_ROOT / "src" / "adapters" / "openai_adapter.py"
CONTRACT = REPO_ROOT / "contracts" / "providers" / "openai" / "openapi.yaml"
OPERATION = PROVIDERS["openai"].operations[0]
# SDK call -> (method, contract path)
SDK_CALLS = {("responses", "create"): ("post", "/responses")}

_ERROR = {"error": {"message": "captured", "type": "invalid_request_error", "param": None, "code": None}}


Query = dict[str, list[str]]   # every query parameter the SDK sent, its values in order


async def _capture(shape) -> tuple[str, str, dict, object, Query]:
    seen: list[httpx.Request] = []

    def sink(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json=_ERROR)

    adapter = OpenAIAdapter(api_key="x")
    adapter.client = AsyncOpenAI(api_key="x", max_retries=0,
                                 http_client=httpx.AsyncClient(transport=httpx.MockTransport(sink)))
    with pytest.raises(LLMClientError):
        await adapter.generate_content(shape.request)
    assert len(seen) == 1, f"{shape.id}: expected exactly one HTTP request, got {len(seen)}"
    request = seen[0]
    query = {name: request.url.params.get_list(name) for name in request.url.params.keys()}
    return request.method, request.url.path, dict(request.headers), json.loads(request.content), query


@functools.lru_cache(maxsize=None)
def _captured(shape_id: str) -> tuple[str, str, dict, object, Query]:
    shape = next(s for s in SHAPES if s.id == shape_id)
    return asyncio.run(_capture(shape))


@pytest.fixture(autouse=True)
def _no_tier_overrides(monkeypatch):
    monkeypatch.delenv("OPENAI_TIER_OVERRIDES", raising=False)


def test_shape_ids_are_unique():
    ids = [s.id for s in SHAPES]
    assert len(ids) == len(set(ids)) and len(ids) > 40


@pytest.mark.parametrize("shape", SHAPES, ids=[s.id for s in SHAPES])
def test_the_request_conforms_to_the_contract(shape):
    method, path, headers, body, query = _captured(shape.id)
    assert_request("openai", method, path, headers, body, query=query)


@pytest.mark.parametrize("shape", SHAPES, ids=[s.id for s in SHAPES])
def test_the_closed_subset_rejects_an_unknown_field(shape):
    method, path, headers, body, query = _captured(shape.id)
    with pytest.raises(AssertionError, match="zz_unknown_field: unknown field"):
        assert_request("openai", method, path, headers, {**body, "zz_unknown_field": 1}, query=query)


@pytest.mark.parametrize("shape", SHAPES, ids=[s.id for s in SHAPES])
def test_the_closed_subset_rejects_an_unknown_query_parameter(shape):
    """The query the SDK sent reaches the check: one the contract does not declare fails."""
    method, path, headers, body, query = _captured(shape.id)
    with pytest.raises(AssertionError, match="query zz_unknown: unknown query parameter"):
        assert_request("openai", method, path, headers, body, query={**query, "zz_unknown": ["1"]})


DEPTHS = ("input[]", "input[].content[]", "tools[]", "text.format", "reasoning")


@pytest.mark.parametrize("depth", DEPTHS)
def test_the_closed_subset_rejects_an_unknown_field_at_depth(depth):
    """An unknown field inside an input item, a content part, a tool, the text format and the reasoning object fails."""
    tried = 0
    for shape in SHAPES:
        method, path, headers, body, query = _captured(shape.id)
        body = copy.deepcopy(body)
        targets = [v for v in _walk(body, depth) if isinstance(v, dict)]
        if not targets:
            continue
        targets[0]["zz_unknown_field"] = 1
        with pytest.raises(AssertionError, match="zz_unknown_field: unknown field"):
            assert_request("openai", method, path, headers, body, query=query)
        tried += 1
    assert tried, f"no shape sends anything at {depth}"


# ------------------------------------------------------------------------------------------ the keep list is used
def _walk(node, path: str):
    """Every value at `path` (`a.b`, `a[]`) of a body; unions are transparent."""
    nodes = [node]
    for step in re.findall(r"[^.\[\]]+|\[\]", path):
        if step == "[]":
            nodes = [i for n in nodes if isinstance(n, list) for i in n]
        else:
            nodes = [n[step] for n in nodes if isinstance(n, dict) and step in n]
    return nodes


def _variant_of(value) -> str:
    if isinstance(value, dict):
        if isinstance(value.get("type"), str):
            return value["type"]
        return "message" if "role" in value else "object"
    return "string" if isinstance(value, str) else type(value).__name__


def _bodies() -> list:
    return [_captured(s.id)[3] for s in SHAPES]


def _contract_body_schema() -> dict:
    doc = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    schema = doc["paths"]["/responses"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    while "$ref" in schema:
        schema = doc["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def test_every_kept_field_is_sent_by_a_shape():
    schema = _contract_body_schema()
    required = set(schema.get("required") or [])
    optional = set(schema["properties"]) - required
    assert set(OPERATION.fields) == set(schema["properties"])
    sent = {k for body in _bodies() for k in body}
    assert sorted(optional - sent) == [], "kept fields no shape sends (dead keep-list entry)"
    assert sorted(required - sent) == []


@pytest.mark.parametrize("path", sorted(OPERATION.variants))
def test_every_kept_variant_is_sent_by_a_shape(path):
    used = {_variant_of(v) for body in _bodies() for v in _walk(body, path)}
    assert sorted(set(OPERATION.variants[path]) - used) == [], f"variants at {path} that no shape sends"


@pytest.mark.parametrize("path", sorted(OPERATION.open))
def test_every_open_path_is_sent_by_a_shape(path):
    assert any(_walk(body, path) for body in _bodies()), path


def test_the_allowed_field_is_sent_by_a_shape():
    for allowed in OPERATION.allowed:
        assert any(allowed.field in body for body in _bodies()), allowed.field


# ---------------------------------------------------------------------------------------- the adapter's call sites
def test_every_sdk_call_site_of_the_adapter_maps_to_an_operation():
    chains = re.findall(r"self\.client((?:\.\w+)+)\(", ADAPTER.read_text(encoding="utf-8"))
    sites = {tuple(c.lstrip(".").split(".")) for c in chains}
    assert sites, "no `self.client...(` call site found: the pattern is stale"
    assert sites <= set(SDK_CALLS), f"call sites with no operation: {sorted(sites - set(SDK_CALLS))}"
    operations = {(op.method, op.path) for op in PROVIDERS["openai"].operations}
    assert {SDK_CALLS[s] for s in sites} <= operations
    paths = set(yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["paths"])
    assert {p for _, p in operations} <= paths
