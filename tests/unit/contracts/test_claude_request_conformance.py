"""Every request ClaudeAdapter builds conforms to Anthropic's request contract (TEST_COVERAGE_SYSTEMIC_RFC §4a, P17).

The REAL `ClaudeAdapter` and the REAL Anthropic SDK run with the HTTP transport replaced (`httpx.MockTransport`): no
network, no key. The sink records method, URL path, headers and the JSON body of each request, and answers 400
`invalid_request_error`, so the adapter raises its client error (the capture short-circuits the call; nothing is
asserted about a response). A shape with `fallback` is answered 529 first, so the adapter's same-provider model fallback
sends its second request, which is checked too. The transport, not the SDK call's kwargs, is the boundary: a kwarg
(`extra_headers`, `timeout`, `stream`) differs from what the SDK puts on the wire.

The contract is `contracts/providers/anthropic/openapi.yaml` (a subset of the SDK's request types, extracted by
`scripts/contracts/extract_subset.py`). The shapes are `claude_request_shapes.py`. Out of this slice: what needs a
provider response (the grounded `container` continuation, replayed raw_content blocks); `NOT_REACHABLE` names each.
"""
from __future__ import annotations

import asyncio
import copy
import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx
import pytest
import yaml
from anthropic import AsyncAnthropic

from scripts.contracts import extract_subset as es
from src.adapters.claude_adapter import ClaudeAdapter
from src.domain.exceptions import LLMClientError
from tests.contracts.openapi_contract import assert_request
from tests.unit.contracts.claude_request_shapes import NOT_REACHABLE, SHAPES

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_FILE = REPO_ROOT / "src" / "adapters" / "claude_adapter.py"
OPERATION = es.PROVIDERS["anthropic"].operations[0]
# `self.client.<a>.<b>(` call sites of the adapter -> the operation each one sends
CALL_SITES = {("messages", "stream"): ("post", "/v1/messages")}


@dataclass(frozen=True)
class Sent:
    method: str
    path: str
    headers: Mapping[str, str]
    body: Any


_OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
_REJECTED = {"type": "error", "error": {"type": "invalid_request_error", "message": "captured by the test"}}


@functools.lru_cache(maxsize=None)
def sent(shape_id: str) -> tuple[Sent, ...]:
    """Run the shape through the real adapter and SDK; the requests that left, in order."""
    shape = SHAPES[shape_id]
    captured: list[Sent] = []

    def sink(request: httpx.Request) -> httpx.Response:
        captured.append(Sent(request.method, request.url.path, dict(request.headers), json.loads(request.content)))
        if shape.fallback and len(captured) == 1:
            return httpx.Response(529, json=_OVERLOADED)
        return httpx.Response(400, json=_REJECTED)

    async def run() -> None:
        adapter = ClaudeAdapter(api_key="x")
        http = httpx.AsyncClient(transport=httpx.MockTransport(sink))
        adapter.client = AsyncAnthropic(api_key="x", max_retries=0, http_client=http)
        try:
            with pytest.raises(LLMClientError):
                await adapter.generate_content(shape.request)
        finally:
            await http.aclose()

    asyncio.run(run())
    assert len(captured) == (2 if shape.fallback else 1), f"{shape_id}: {len(captured)} requests left"
    return tuple(captured)


def all_sent() -> list[Sent]:
    return [s for shape_id in SHAPES for s in sent(shape_id)]


# ---------------------------------------------------------------------------------------------------- conformance
@pytest.mark.parametrize("shape_id", sorted(SHAPES))
def test_every_request_the_adapter_builds_conforms_to_the_contract(shape_id):
    for request in sent(shape_id):
        assert_request("anthropic", request.method, request.path, request.headers, request.body)


@pytest.mark.parametrize("shape_id", sorted(SHAPES))
def test_the_subset_is_closed_an_unknown_field_fails(shape_id):
    request = sent(shape_id)[0]
    top = {**request.body, "not_a_field_of_the_contract": 1}
    with pytest.raises(AssertionError, match=r"\$\.not_a_field_of_the_contract: unknown field"):
        assert_request("anthropic", request.method, request.path, request.headers, top)
    nested = copy.deepcopy(request.body)
    nested["messages"][0]["content"][0]["not_a_field_of_the_contract"] = 1
    with pytest.raises(AssertionError, match=r"messages\[0\]\.content\[0\]\.not_a_field_of_the_contract: unknown field"):
        assert_request("anthropic", request.method, request.path, request.headers, nested)


def test_a_shape_with_a_fallback_sends_the_next_model_down():
    for shape_id, shape in SHAPES.items():
        if shape.fallback:
            first, second = sent(shape_id)
            assert first.body["model"] == shape.request.model_name
            assert second.body["model"] != first.body["model"], shape_id


# ---------------------------------------------------------------------------------------------- nothing dead in the keep list
def _values_at(node: Any, path: str) -> list[Any]:
    """The values at `a.b[]` in a body (`[]` walks an array's items); missing parts yield nothing."""
    nodes = [node]
    for step in path.split("."):
        many = step.endswith("[]")
        key = step[:-2] if many else step
        nxt: list[Any] = []
        for n in nodes:
            if isinstance(n, dict) and key in n:
                v = n[key]
                nxt.extend(v if many and isinstance(v, list) else [v])
        nodes = nxt
    return nodes


def _tag(value: Any, path: str) -> str | None:
    """The variant a value is: its `type`; a custom tool has none; a string is `string`."""
    if isinstance(value, dict):
        return value.get("type", "custom" if path == "tools[]" else None)
    return None


def _reached() -> set[str]:
    reached: set[str] = set()
    for request in all_sent():
        reached.update(request.body)
        for path in OPERATION.variants:
            for value in _values_at(request.body, path):
                if (tag := _tag(value, path)) is not None:
                    reached.add(f"{path}:{tag}")
    return reached


def _required_fields() -> set[str]:
    doc = yaml.safe_load((REPO_ROOT / "contracts" / "providers" / "anthropic" / "openapi.yaml").read_text("utf-8"))
    schema = doc["paths"]["/v1/messages"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    while "$ref" in schema:
        schema = doc["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return set(schema.get("required") or [])


def test_every_kept_field_and_variant_is_used_by_a_shape():
    wanted = {f for f in OPERATION.fields if f not in _required_fields()}
    wanted |= {f"{path}:{v}" for path, variants in OPERATION.variants.items() for v in variants}
    wanted -= set(NOT_REACHABLE)
    missing = sorted(wanted - _reached())
    assert not missing, f"keep-list entries no shape reaches (dead entry or a missing shape): {missing}"


def test_the_not_reachable_list_has_no_stale_entry():
    kept = set(OPERATION.fields) | {f"{p}:{v}" for p, vs in OPERATION.variants.items() for v in vs}
    assert set(NOT_REACHABLE) <= kept, sorted(set(NOT_REACHABLE) - kept)
    assert not set(NOT_REACHABLE) & _reached(), sorted(set(NOT_REACHABLE) & _reached())


def test_every_allowed_gap_is_sent_by_a_shape():
    """An ALLOWED entry no shape sends is dead: the adapter no longer needs it."""
    for allowed in OPERATION.allowed:
        if allowed.path.startswith("header:"):
            name = allowed.path[len("header:"):]
            sent_values = {
                v.strip() for r in all_sent() for v in r.headers.get(name, "").split(",") if v.strip()
            }
            assert allowed.value in sent_values, allowed
        elif allowed.value is not None:
            assert allowed.value in {
                _tag(v, allowed.path) for r in all_sent() for v in _values_at(r.body, allowed.path)
            }, allowed
        else:
            assert any(allowed.field in v for r in all_sent() for v in _values_at(r.body, allowed.path or "")
                       if isinstance(v, dict)) or allowed.path == "", allowed


# ------------------------------------------------------------------------------------------------- call sites
def test_every_sdk_call_site_of_the_adapter_maps_to_an_operation():
    found = set(re.findall(r"self\.client\.(\w+)\.(\w+)\(", ADAPTER_FILE.read_text(encoding="utf-8")))
    assert found, "no `self.client.<a>.<b>(` call site found: the pattern or the adapter changed"
    unmapped = sorted(found - set(CALL_SITES))
    assert not unmapped, f"adapter call sites without an Operation in PROVIDERS (add one, with shapes): {unmapped}"
    for site in found:
        method, path = CALL_SITES[site]
        assert (OPERATION.method, OPERATION.path) == (method, path), site
    # and the request each site sends is the one the shapes run through
    assert {(r.method.lower(), r.path) for r in all_sent()} == {(OPERATION.method, OPERATION.path)}
