"""The response side of the OpenAI contract: what `POST /responses` returns, and what OpenAIAdapter reads of it (P17).

`openai_response_samples.SAMPLES` are response bodies as the wire carries them. Each one:
- conforms to the response schema of `contracts/providers/openai/openapi.yaml` (`assert_response`);
- goes through the REAL SDK and the REAL adapter (`httpx.MockTransport` answers with it) and comes out as the
  `LLMResponse` the adapter builds (text, citations, tool calls, finish reason, usage legs);
- passed back as `raw_content` in the next request (what the delegation loop does), conforms to the REQUEST contract.

Meta-tests: an unknown field and a wrong-typed field fail at the top level and at depth; every kept top-level field and
every kept union variant of the response is exercised by a sample (a dead keep-list entry fails); every ALLOWED
response gap is carried by a sample or a committed recording.
"""
from __future__ import annotations

import asyncio
import copy
import functools
import json
import re

import httpx
import pytest
from openai import AsyncOpenAI

from scripts.contracts.extract_subset import PROVIDERS
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.exceptions import LLMClientError
from src.domain.llm import FinishReason
from src.ports.llm_port import LLMRequest, LLMResponse, Message, MessagePart
from tests.contracts.openapi_contract import assert_request, assert_response
from tests.unit.contracts.allowed_gaps import carries
from tests.unit.contracts.openai_response_samples import CACHE_USAGE, MODEL, SAMPLES
from tests.unit.contracts.recorded_exchanges import EXCHANGES, provider_of, recording

OPERATION = PROVIDERS["openai"].operations[0]
(RESPONSE,) = OPERATION.responses
_ERROR = {"error": {"message": "captured", "type": "invalid_request_error", "param": None, "code": None}}


@pytest.fixture(autouse=True)
def _no_tier_overrides(monkeypatch):
    monkeypatch.delenv("OPENAI_TIER_OVERRIDES", raising=False)


def _request(messages: list[Message] | None = None) -> LLMRequest:
    return LLMRequest(model_name=MODEL, messages=messages or [
        Message(role="user", parts=[MessagePart(text="When is the car service?")])])


async def _run(handler, request: LLMRequest) -> LLMResponse:
    adapter = OpenAIAdapter(api_key="x")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter.client = AsyncOpenAI(api_key="x", max_retries=0, http_client=http)
    try:
        return await adapter.generate_content(request)
    finally:
        await http.aclose()


@functools.lru_cache(maxsize=None)
def parsed(sample_id: str) -> LLMResponse:
    """The sample answered by the transport, read by the real SDK and the real adapter."""
    return asyncio.run(_run(lambda request: httpx.Response(200, json=SAMPLES[sample_id]), _request()))


# ---------------------------------------------------------------------------------------------------- conformance
@pytest.mark.parametrize("sample_id", sorted(SAMPLES))
def test_every_sample_conforms_to_the_response_contract(sample_id):
    assert_response("openai", "POST", "/responses", 200, SAMPLES[sample_id])


@pytest.mark.parametrize("sample_id", sorted(SAMPLES))
def test_every_sample_is_read_by_the_real_sdk_and_adapter(sample_id):
    response = parsed(sample_id)
    assert isinstance(response, LLMResponse) and response.usage_metadata is not None
    assert response.raw_content is not None and len(response.raw_content) == len(SAMPLES[sample_id]["output"])


def test_the_adapter_reads_the_text_and_the_finish_reason():
    response = parsed("text")
    assert response.text == SAMPLES["text"]["output"][0]["content"][0]["text"]
    assert response.finish_reason == FinishReason.STOP and response.tool_calls == []


def test_the_adapter_reads_a_function_call():
    (call,) = parsed("function_call_after_reasoning").tool_calls
    assert (call.name, call.args, call.thought_signature) == ("lookup_value", {"key": "car service"}, "call_sample")


def test_the_adapter_appends_the_url_citations_as_sources():
    text = parsed("grounded_with_citations").text
    assert text.startswith("Sao Joao is celebrated in Porto on 24 June.")
    assert text.endswith("*Sources:*\n- [Porto festivals](https://example.com/festivals/porto)")


def test_the_adapter_reads_an_incomplete_response_and_a_truncated_call():
    response = parsed("incomplete_truncated_call")
    assert response.finish_reason == FinishReason.MAX_TOKENS
    assert response.tool_calls[0].args["_parse_error"] == "truncated_json"


def test_the_adapter_reads_the_three_disjoint_usage_legs():
    usage = parsed("cache_read_and_write").usage_metadata
    cached = CACHE_USAGE["input_tokens_details"]["cached_tokens"]
    written = CACHE_USAGE["input_tokens_details"]["cache_write_tokens"]
    assert (usage.prompt_tokens, usage.cache_read_tokens, usage.cache_creation_tokens, usage.completion_tokens) == (
        CACHE_USAGE["input_tokens"] - cached - written, cached, written, CACHE_USAGE["output_tokens"])


@pytest.mark.parametrize("sample_id", sorted(SAMPLES))
def test_the_output_items_passed_back_conform_to_the_request_contract(sample_id):
    """The delegation loop sends a response's output items back as the next request's input (raw_content)."""
    response = parsed(sample_id)
    follow_up: list[Message] = [
        Message(role="user", parts=[MessagePart(text="When is the car service?")]),
        Message(role="model", parts=[MessagePart(text=response.text or "")], raw_content=response.raw_content),
    ]
    follow_up += [Message(role="user", parts=[MessagePart(tool_response={"name": c.name, "response": "2026-11-05"})])
                  for c in response.tool_calls]
    if not response.tool_calls:
        follow_up.append(Message(role="user", parts=[MessagePart(text="Thanks.")]))
    seen: list[httpx.Request] = []

    def sink(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json=_ERROR)

    with pytest.raises(LLMClientError):
        asyncio.run(_run(sink, _request(follow_up)))
    (request,) = seen
    assert_request("openai", request.method, request.url.path, dict(request.headers), json.loads(request.content),
                   query={k: request.url.params.get_list(k) for k in request.url.params.keys()})


# ----------------------------------------------------------------------------------------- unknown and wrong fields
def _violations(body) -> list[str]:
    """The violations `assert_response` lists ([] when it passes). Any other failure (an operation not found, no
    response for the status) propagates: read as [], it would make a test asserting [] pass vacuously."""
    try:
        assert_response("openai", "POST", "/responses", 200, body)
    except AssertionError as failure:
        head, *violations = str(failure).splitlines()
        if not head.endswith(" breaks the contract:") or not violations:
            raise
        return violations
    return []


def _at(body: dict, path: str) -> dict:
    """The first object at `path` (`output[type=message].content[0]`-like steps: `key`, `key[i]`, `key[type=t]`)."""
    node = body
    for step in path.split("."):
        name, _, index = step.partition("[")
        node = node[name]
        if index:
            index = index.rstrip("]")
            node = next(i for i in node if i.get("type") == index[5:]) if index.startswith("type=") else node[int(index)]
    return node


DEPTHS = {
    "output[type=message]": ("grounded_with_citations", r"\$\.output\[4\]\.zz: unknown field"),
    "output[type=message].content[0]": ("text", r"\$\.output\[0\]\.content\[0\]\.zz: unknown field"),
    "output[type=message].content[0].annotations[0]":
        ("grounded_with_citations", r"\$\.output\[4\]\.content\[0\]\.annotations\[0\]\.zz: unknown field"),
    "output[type=function_call]": ("function_call_after_reasoning", r"\$\.output\[1\]\.zz: unknown field"),
    "output[type=reasoning].summary[0]": ("function_call_after_reasoning", r"\$\.output\[0\]\.summary\[0\]\.zz"),
    "output[type=web_search_call].action": ("grounded_with_citations", r"\$\.output\[1\]\.action\.zz: unknown field"),
    "usage": ("text", r"\$\.usage\.zz: unknown field"),
    "usage.input_tokens_details": ("cache_read_and_write", r"\$\.usage\.input_tokens_details\.zz: unknown field"),
    "usage.output_tokens_details": ("text", r"\$\.usage\.output_tokens_details\.zz: unknown field"),
}


def test_an_unknown_top_level_field_fails():
    assert _violations({**SAMPLES["text"], "zz": 1}) == ["$.zz: unknown field"]


@pytest.mark.parametrize("path", sorted(DEPTHS))
def test_an_unknown_field_at_depth_fails(path):
    sample_id, message = DEPTHS[path]
    body = copy.deepcopy(SAMPLES[sample_id])
    _at(body, path)["zz"] = 1
    problems = _violations(body)
    assert len(problems) == 1 and re.fullmatch(message + ".*", problems[0]), problems


@pytest.mark.parametrize("path, value, expected", [
    ("status", "done", "$.status: value not allowed ('done')"),
    ("model", 7, "$.model: wrong type (expected string)"),
    ("usage.input_tokens", "many", "$.usage.input_tokens: wrong type (expected integer)"),
    ("usage.input_tokens_details.cached_tokens", None, "$.usage.input_tokens_details.cached_tokens: wrong type "
                                                       "(expected integer)"),
    ("output[0].content[0].text", 5, "$.output[0].content[0].text: wrong type (expected string)"),
])
def test_a_wrong_type_fails_at_the_top_level_and_at_depth(path, value, expected):
    body = copy.deepcopy(SAMPLES["text"])
    *parents, leaf = path.split(".")
    (_at(body, ".".join(parents)) if parents else body)[leaf] = value
    assert _violations(body) == [expected]


def test_an_output_item_type_the_contract_does_not_keep_fails():
    body = {**SAMPLES["text"], "output": [{"type": "file_search_call", "id": "fs_1", "status": "completed",
                                           "queries": []}]}
    assert _violations(body) == ["$.output[0].type: value not allowed ('file_search_call')"]


# ------------------------------------------------------------------------------------ nothing dead in the keep list
def _values_at(node, path: str) -> list:
    nodes = [node]
    for step in path.split("."):
        many = step.endswith("[]")
        key = step[:-2] if many else step
        nodes = [x for n in nodes if isinstance(n, dict) and key in n
                 for x in (n[key] if many and isinstance(n[key], list) else [n[key]])]
    return nodes


def test_every_kept_top_level_field_is_in_a_sample():
    missing = set(RESPONSE.fields) - {k for body in SAMPLES.values() for k in body}
    assert not missing, f"kept response fields no sample carries: {sorted(missing)}"


@pytest.mark.parametrize("path", sorted(RESPONSE.variants))
def test_every_kept_variant_is_in_a_sample(path):
    used = {v.get("type") for body in SAMPLES.values() for v in _values_at(body, path) if isinstance(v, dict)}
    assert sorted(set(RESPONSE.variants[path]) - used) == [], f"variants at {path} no sample carries"


def test_every_response_allowed_gap_is_in_a_sample_or_a_recording():
    """A response gap is evidenced by a body that carries it where the contract puts it: a sample, or a committed
    recording (the provider's real answer; `recorded_exchanges.py`). A gap no body carries is dead: the provider no
    longer sends it, so the entry goes."""
    recorded = [recording(p)["exchanges"][n]["response"]["body"] for p, n in EXCHANGES if provider_of(p) == "openai"]
    assert recorded, "no OpenAI recording: the recorded half of the evidence is missing"
    bodies = [*SAMPLES.values(), *recorded]
    dead = [allowed for allowed in RESPONSE.allowed if not any(carries(allowed, body) for body in bodies)]
    assert not dead, f"ALLOWED response gaps no sample or recording carries: {dead}"
