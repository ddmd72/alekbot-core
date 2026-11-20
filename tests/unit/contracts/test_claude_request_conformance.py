"""Every request ClaudeAdapter builds conforms to Anthropic's request contract (TEST_COVERAGE_SYSTEMIC_RFC §4a, P17).

The REAL `ClaudeAdapter` and the REAL Anthropic SDK run with the HTTP transport replaced (`httpx.MockTransport`): no
network, no key. The sink records method, URL path, query, headers and the JSON body of each request, and answers 400
`invalid_request_error`, so the adapter raises its client error (the capture short-circuits the call; nothing is
asserted about a response). A shape with `fallback` is answered 529 first, so the adapter's same-provider model fallback
sends its second request, which is checked too. The transport, not the SDK call's kwargs, is the boundary: a kwarg
(`extra_headers`, `timeout`, `stream`) differs from what the SDK puts on the wire.

The contract is `contracts/providers/anthropic/openapi.yaml` (a subset of the SDK's request types, extracted by
`scripts/contracts/extract_subset.py`). The shapes are `claude_request_shapes.py`. Out of this slice: the grounded
`container` continuation (it needs a streamed pause_turn response); `NOT_REACHABLE` names it.
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
from tests.unit.contracts.allowed_gaps import carries
from tests.unit.contracts.claude_request_shapes import NOT_REACHABLE, SHAPES
from tests.unit.contracts.recorded_exchanges import EXCHANGES, provider_of, recording

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
    query: Mapping[str, tuple[str, ...]]   # every query parameter the SDK sent, its values in order


_OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
_REJECTED = {"type": "error", "error": {"type": "invalid_request_error", "message": "captured by the test"}}


@functools.lru_cache(maxsize=None)
def sent(shape_id: str) -> tuple[Sent, ...]:
    """Run the shape through the real adapter and SDK; the requests that left, in order."""
    shape = SHAPES[shape_id]
    captured: list[Sent] = []

    def sink(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        captured.append(Sent(request.method, request.url.path, dict(request.headers), json.loads(request.content),
                             {name: tuple(params.get_list(name)) for name in params.keys()}))
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


def _check(request: Sent, body: Any = None, query: Mapping[str, tuple[str, ...]] | None = None) -> None:
    """assert_request on what the SDK sent, the query included; `body`/`query` replace the sent ones."""
    assert_request("anthropic", request.method, request.path, request.headers,
                   request.body if body is None else body,
                   query={name: list(values) for name, values in (request.query if query is None else query).items()})


# ---------------------------------------------------------------------------------------------------- conformance
@pytest.mark.parametrize("shape_id", sorted(SHAPES))
def test_every_request_the_adapter_builds_conforms_to_the_contract(shape_id):
    for request in sent(shape_id):
        _check(request)


@pytest.mark.parametrize("shape_id", sorted(SHAPES))
def test_the_subset_is_closed_an_unknown_field_fails(shape_id):
    request = sent(shape_id)[0]
    top = {**request.body, "not_a_field_of_the_contract": 1}
    with pytest.raises(AssertionError, match=r"\$\.not_a_field_of_the_contract: unknown field"):
        _check(request, body=top)
    nested = copy.deepcopy(request.body)
    nested["messages"][0]["content"][0]["not_a_field_of_the_contract"] = 1
    with pytest.raises(AssertionError, match=r"messages\[0\]\.content\[0\]\.not_a_field_of_the_contract: unknown field"):
        _check(request, body=nested)


@pytest.mark.parametrize("shape_id", sorted(SHAPES))
def test_the_subset_is_closed_an_unknown_query_parameter_fails(shape_id):
    """The query the SDK sent reaches the check: one the contract does not declare fails."""
    for request in sent(shape_id):
        with pytest.raises(AssertionError, match="query zz_unknown: unknown query parameter"):
            _check(request, query={**request.query, "zz_unknown": ("1",)})


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


def _recorded_requests() -> list[tuple[Mapping[str, str], Any]]:
    """The committed Anthropic recordings' requests as (headers, body): what the current adapter sends, since
    `test_recorded_exchanges_replay.py` fails when its requests differ from them."""
    return [({k.lower(): v for k, v in ex["request"]["headers"].items()}, ex["request"]["body"])
            for p, n in EXCHANGES if provider_of(p) == "anthropic" for ex in [recording(p)["exchanges"][n]]]


def test_every_allowed_gap_is_sent_by_a_shape_or_a_recording():
    """An ALLOWED entry neither a shape nor a committed recording sends is dead: the adapter no longer needs it. A
    recording counts: its replay proves the adapter still sends it, and it holds what no offline shape builds (a block
    the real stream assembled, P17-D1)."""
    recorded = _recorded_requests()
    assert recorded, "no Anthropic recording: the recorded half of the evidence is missing"
    sent_requests = [(r.headers, r.body) for r in all_sent()] + recorded
    for allowed in OPERATION.allowed:
        if allowed.path.startswith("header:"):
            name = allowed.path[len("header:"):]
            sent_values = {
                v.strip() for headers, _ in sent_requests for v in headers.get(name, "").split(",") if v.strip()
            }
            assert allowed.value in sent_values, allowed
        elif allowed.value is not None and allowed.variant is None:
            assert allowed.value in {
                _tag(v, allowed.path) for _, body in sent_requests for v in _values_at(body, allowed.path)
            }, allowed
        else:
            assert any(carries(allowed, body) for _, body in sent_requests), allowed

# ------------------------------------------------------------------------------------------------- call sites
def _call_sites(source: str) -> set[tuple[str, ...]]:
    """Every `self.client.<a>[.<b>...](` call chain in the adapter source, as its names after `client`."""
    return {tuple(m.group(1).lstrip(".").split(".")) for m in re.finditer(r"self\.client((?:\.\w+)+)\(", source)}


def test_every_sdk_call_site_of_the_adapter_maps_to_an_operation():
    found = set(_call_sites(ADAPTER_FILE.read_text(encoding="utf-8")))
    assert found, "no `self.client...(` call site found: the pattern or the adapter changed"
    unmapped = sorted(found - set(CALL_SITES))
    assert not unmapped, f"adapter call sites without an Operation in PROVIDERS (add one, with shapes): {unmapped}"
    for site in found:
        method, path = CALL_SITES[site]
        assert (OPERATION.method, OPERATION.path) == (method, path), site
    # and the request each site sends is the one the shapes run through
    assert {(r.method.lower(), r.path) for r in all_sent()} == {(OPERATION.method, OPERATION.path)}
