"""The response side of the Anthropic contract: what `POST /v1/messages` returns, and what ClaudeAdapter reads (P17).

ClaudeAdapter always streams, so two shapes are checked against `contracts/providers/anthropic/openapi.yaml`:
the server-sent events on the wire (`text/event-stream`, one schema per event's data) and the whole message the SDK
assembles from them (`application/json`, what `_parse_response` reads). `claude_response_samples.SAMPLES` are whole
messages; `events()` renders each as its stream. Each sample:
- conforms as a message and as a stream;
- streamed through the REAL SDK and the REAL adapter (`httpx.MockTransport`), comes out as the `LLMResponse` the adapter
  builds, and the message the SDK assembled conforms again;
- replayed as `raw_content` in the next request, conforms to the REQUEST contract.
The grounded loop's pause_turn continuation runs offline too: its second request carries `container` (the request-side
`NOT_REACHABLE` entry) and is checked against the request contract.

Meta-tests: an unknown field and a wrong-typed field fail at the top level and at depth, in a message and in an event;
every kept top-level field and union variant of both responses is exercised (a dead keep-list entry fails), and every
ALLOWED gap is carried by a sample or, for the stream, a committed recording.
"""
from __future__ import annotations

import asyncio
import copy
import functools
import json

import httpx
import pytest
from anthropic import AsyncAnthropic

from scripts.contracts.extract_subset import PROVIDERS
from src.adapters.claude_adapter import ClaudeAdapter
from src.domain.exceptions import LLMClientError, LLMRefusalError
from src.domain.llm import FinishReason
from src.ports.llm_port import LLMRequest, LLMResponse, Message, MessagePart
from tests.contracts.openapi_contract import assert_request, assert_response, assert_stream
from tests.unit.contracts import claude_response_samples as cs
from tests.unit.contracts.allowed_gaps import carries
from tests.unit.contracts.claude_response_samples import SAMPLES, events, sse
from tests.unit.contracts.recorded_exchanges import EXCHANGES, provider_of, recording

OPERATION = PROVIDERS["anthropic"].operations[0]
MESSAGE, STREAM = OPERATION.responses
PATH = "/v1/messages"
_REJECTED = {"type": "error", "error": {"type": "invalid_request_error", "message": "captured by the test"}}


class PreconditionFailed(Exception):
    """Not an AssertionError: under a strict `xfail(raises=AssertionError)` a broken setup reports FAILED, never a
    silent XFAIL (the `require` convention of tests/CLAUDE.md; tests/support/redelivery_world.py is too heavy to import
    here, about 8 s)."""


def require(cond: bool, what: str) -> None:
    if not cond:
        raise PreconditionFailed(what)


# Kept variant paths no realistic stream exercises, with the reason.
NOT_EXERCISED = {
    "message.content[]": "message_start carries the message before any block: its content list is empty on the wire",
}


def _streamed(body: dict) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events(body)))


def _request(model: str = cs.MODEL, **kw) -> LLMRequest:
    kw.setdefault("messages", [Message(role="user", parts=[MessagePart(text="When is the car service?")])])
    return LLMRequest(model_name=model, **kw)


async def _run(handler, request: LLMRequest) -> LLMResponse:
    adapter = ClaudeAdapter(api_key="x")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter.client = AsyncAnthropic(api_key="x", max_retries=0, http_client=http)
    try:
        return await adapter.generate_content(request)
    finally:
        await http.aclose()


@functools.lru_cache(maxsize=None)
def parsed(sample_id: str) -> LLMResponse:
    """The sample streamed by the transport, assembled by the real SDK, read by the real adapter."""
    return asyncio.run(_run(lambda request: _streamed(SAMPLES[sample_id]), _request()))


READABLE = sorted(s for s in SAMPLES if s not in ("refusal", "grounded_server_tools_paused"))


# ---------------------------------------------------------------------------------------------------- conformance
@pytest.mark.parametrize("sample_id", sorted(SAMPLES))
def test_every_sample_conforms_as_a_message(sample_id):
    assert_response("anthropic", "POST", PATH, 200, SAMPLES[sample_id])


@pytest.mark.parametrize("sample_id", sorted(SAMPLES))
def test_every_sample_conforms_as_a_stream(sample_id):
    assert_stream("anthropic", "POST", PATH, 200, events(SAMPLES[sample_id]))


@pytest.mark.parametrize("sample_id", READABLE)
def test_the_message_the_sdk_assembles_from_the_stream_conforms(sample_id):
    raw = parsed(sample_id).raw_content
    assert_response("anthropic", "POST", PATH, 200, raw.to_dict())
    assert [b.type for b in raw.content] == [b["type"] for b in SAMPLES[sample_id]["content"]]


def test_the_adapter_reads_the_text_and_the_finish_reason():
    response = parsed("text")
    assert response.text == SAMPLES["text"]["content"][0]["text"]
    assert response.finish_reason == FinishReason.STOP and response.tool_calls == []


def test_the_adapter_reads_a_tool_call_and_skips_thinking():
    response = parsed("tool_use_after_thinking")
    (call,) = response.tool_calls
    assert (call.name, call.args, call.thought_signature) == ("lookup_value", {"key": "car service"},
                                                              "toolu_sample")
    assert response.text == "Let me look that up."


def test_the_adapter_reads_a_truncated_answer():
    assert parsed("max_tokens").finish_reason == FinishReason.MAX_TOKENS


def test_the_adapter_reads_the_cache_legs():
    usage = parsed("cache_read_and_write").usage_metadata
    assert (usage.prompt_tokens, usage.cache_read_tokens, usage.cache_creation_tokens, usage.completion_tokens) == (
        cs.CACHE_USAGE["input_tokens"], cs.CACHE_USAGE["cache_read_input_tokens"],
        cs.CACHE_USAGE["cache_creation_input_tokens"], cs.CACHE_USAGE["output_tokens"])


def test_the_adapter_raises_on_a_refusal_with_its_details():
    with pytest.raises(LLMRefusalError, match="category=general_harms"):
        asyncio.run(_run(lambda request: _streamed(SAMPLES["refusal"]), _request()))


def _grounded_run() -> tuple[LLMResponse, list[dict], list[httpx.Request]]:
    sent: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _streamed(SAMPLES["grounded_server_tools_paused"] if len(sent) == 1 else cs.GROUNDED_FINAL)

    response = asyncio.run(_run(provider, _request(cs.GROUNDED_MODEL, use_grounding=True)))
    return response, [json.loads(r.content) for r in sent], sent


def test_the_grounded_loop_continues_a_paused_turn_in_its_container():
    response, (first, second), sent = _grounded_run()
    assert response.text == cs.GROUNDED_FINAL["content"][0]["text"]
    assert response.usage_metadata.prompt_tokens == (SAMPLES["grounded_server_tools_paused"]["usage"]["input_tokens"]
                                                     + cs.GROUNDED_FINAL["usage"]["input_tokens"])
    assert "container" not in first and second["container"] == cs.CONTAINER["id"]
    replayed = [b["type"] for b in second["messages"][-1]["content"]]
    assert replayed == [b["type"] for b in SAMPLES["grounded_server_tools_paused"]["content"]]
    assert_request("anthropic", sent[0].method, sent[0].url.path, dict(sent[0].headers), first,
                   query={k: sent[0].url.params.get_list(k) for k in sent[0].url.params.keys()})


# P17-D1: the SDK's stream accumulator builds every block of content_block_start with `model_dump()` (not
# exclude_unset; anthropic/lib/streaming/_messages.py accumulate_event), so a tool_use or server_tool_use block whose
# start event has no `caller` gets `caller: None` set, and replaying the block (raw_content, the pause_turn
# continuation) sends `"caller": null`. The recordings settled it for `server_tool_use` only (replay_server_tool_blocks:
# null sent, answered 200), now an ALLOWED gap narrowed to that block. On the real wire a `tool_use` and a server-tool
# result block arrive with `caller: {type: direct}` (replay_thinking_and_tool_use, replay_server_tool_blocks), so no
# recording shows null there and the contract keeps refusing it; these samples' start events carry no caller, which is
# why the cases below still fail, on those blocks alone.
_P17_D1 = pytest.mark.xfail(strict=True, raises=AssertionError,
                            reason="P17-D1: a replayed tool block carries caller: null, absent from the request contract")


@_P17_D1
def test_the_continuation_of_a_paused_turn_conforms_to_the_request_contract():
    _, (_, second), sent = _grounded_run()
    require("container" in second and len(second["messages"][-1]["content"]) == len(
        SAMPLES["grounded_server_tools_paused"]["content"]), "the continuation replays the paused turn")
    assert_request("anthropic", sent[1].method, sent[1].url.path, dict(sent[1].headers), second,
                   query={k: sent[1].url.params.get_list(k) for k in sent[1].url.params.keys()})


def _has_tool_block(sample_id: str) -> bool:
    return any(b["type"] in ("tool_use", "server_tool_use") for b in SAMPLES[sample_id]["content"])


@pytest.mark.parametrize("sample_id", [
    pytest.param(s, marks=_P17_D1) if _has_tool_block(s) else s for s in READABLE])
def test_the_message_replayed_as_raw_content_conforms_to_the_request_contract(sample_id):
    """The delegation loop sends the assistant turn back unchanged (raw_content) with the tool results after it."""
    response = parsed(sample_id)
    follow_up = [
        Message(role="user", parts=[MessagePart(text="When is the car service?")]),
        Message(role="model", parts=[MessagePart(text=response.text or "")], raw_content=response.raw_content),
        Message(role="user", parts=[MessagePart(tool_response={"name": c.name, "response": "2026-11-05",
                                                               "tool_use_id": c.thought_signature})
                                    for c in response.tool_calls] or [MessagePart(text="Thanks.")]),
    ]
    sent: list[httpx.Request] = []

    def sink(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(400, json=_REJECTED)

    tools = [{"name": "lookup_value", "description": "Look a value up.",
              "parameters": {"type": "object", "properties": {"key": {"type": "string"}}}}]
    try:
        asyncio.run(_run(sink, _request(messages=follow_up, tools=tools)))
    except LLMClientError:
        pass
    require(len(sent) == 1, "exactly one request left")
    body = json.loads(sent[0].content)
    require([b["type"] for b in body["messages"][1]["content"]] == [b["type"] for b in SAMPLES[sample_id]["content"]],
            "the assistant turn is replayed block for block")
    assert_request("anthropic", sent[0].method, sent[0].url.path, dict(sent[0].headers), body,
                   query={k: sent[0].url.params.get_list(k) for k in sent[0].url.params.keys()})


def test_the_replayed_tool_block_carries_caller_null_p17_d1():
    """Pins the mechanism of P17-D1 (flips when the SDK or the adapter stops sending it)."""
    block = parsed("tool_use_after_thinking").raw_content.content[3]
    assert block.type == "tool_use" and "caller" in block.model_fields_set and block.caller is None


# ----------------------------------------------------------------------------------------- unknown and wrong fields
def _violations(body, media: str = "application/json") -> list[str]:
    """The violations `assert_response` lists ([] when it passes). Any other failure (an operation not found, no
    response for the status or media type) propagates: read as [], it would make a test asserting [] pass vacuously."""
    try:
        assert_response("anthropic", "POST", PATH, 200, body, media=media)
    except AssertionError as failure:
        head, *violations = str(failure).splitlines()
        if not head.endswith(" breaks the contract:") or not violations:
            raise
        return violations
    return []


def _set(body: dict, path: tuple, key: str, value) -> dict:
    body = copy.deepcopy(body)
    node = body
    for step in path:
        node = node[step]
    node[key] = value
    return body


@pytest.mark.parametrize("sample_id, path, where", [
    ("text", (), "$"),
    ("text", ("content", 0), "$.content[0]"),
    ("grounded_with_citations", ("content", 2, "citations", 0), "$.content[2].citations[0]"),
    ("grounded_with_citations", ("content", 1, "content", 0), "$.content[1].content[0]"),
    ("grounded_server_tools_paused", ("content", 1, "content", "content", "source"),
     "$.content[1].content.content.source"),
    ("grounded_server_tools_paused", ("content", 5, "content"), "$.content[5].content"),
    ("tool_use_after_thinking", ("content", 0), "$.content[0]"),
    ("cache_read_and_write", ("usage",), "$.usage"),
    ("cache_read_and_write", ("usage", "cache_creation"), "$.usage.cache_creation"),
    ("refusal", ("stop_details",), "$.stop_details"),
])
def test_an_unknown_field_in_a_message_fails_at_the_top_level_and_at_depth(sample_id, path, where):
    assert _violations(_set(SAMPLES[sample_id], path, "zz", 1)) == [f"{where}.zz: unknown field"]


@pytest.mark.parametrize("sample_id, path, key, value, expected", [
    ("text", (), "stop_reason", "done", "$.stop_reason: value not allowed ('done')"),
    ("text", (), "model", 5, "$.model: wrong type (expected string)"),
    ("text", ("content", 0), "text", 5, "$.content[0].text: wrong type (expected string)"),
    ("text", ("usage",), "input_tokens", "many", "$.usage.input_tokens: wrong type (expected integer)"),
    ("tool_use_after_thinking", ("content", 3), "input", "{}", "$.content[3].input: wrong type (expected object)"),
])
def test_a_wrong_type_in_a_message_fails_at_the_top_level_and_at_depth(sample_id, path, key, value, expected):
    assert _violations(_set(SAMPLES[sample_id], path, key, value)) == [expected]


def test_a_block_type_the_contract_does_not_keep_fails():
    body = _set(SAMPLES["text"], (), "content", [{"type": "container_upload", "file_id": "f1"}])
    assert _violations(body) == ["$.content[0].type: value not allowed ('container_upload')"]


def _event(sample_id: str, name: str) -> dict:
    return next(e["data"] for e in events(SAMPLES[sample_id]) if e["event"] == name)


@pytest.mark.parametrize("name, path, where", [
    ("message_start", (), "$"),
    ("message_start", ("message",), "$.message"),
    ("message_start", ("message", "usage"), "$.message.usage"),
    ("content_block_start", ("content_block",), "$.content_block"),
    ("content_block_delta", ("delta",), "$.delta"),
    ("message_delta", ("delta",), "$.delta"),
    ("message_delta", ("usage",), "$.usage"),
])
def test_an_unknown_field_in_an_event_fails_at_the_top_level_and_at_depth(name, path, where):
    data = _set(_event("grounded_with_citations", name), path, "zz", 1)
    assert _violations(data, "text/event-stream") == [f"{where}.zz: unknown field"]


def test_a_wrong_type_in_an_event_fails_at_the_top_level_and_at_depth():
    assert _violations({"type": "content_block_stop", "index": "0"}, "text/event-stream") == [
        "$.index: wrong type (expected integer)"]
    data = _set(_event("text", "content_block_delta"), ("delta",), "text", 5)
    assert _violations(data, "text/event-stream") == ["$.delta.text: wrong type (expected string)"]
    assert _violations({"type": "error_storm"}, "text/event-stream") == ["$.type: value not allowed ('error_storm')"]


def test_the_container_id_is_where_the_grounded_loop_reads_it():
    delta = _event("grounded_server_tools_paused", "message_delta")["delta"]
    assert delta["container"]["id"] == cs.CONTAINER["id"]
    assert _violations(_set({"type": "message_delta", "delta": delta, "usage": {"output_tokens": 1}},
                            ("delta", "container"), "id", 7), "text/event-stream") == [
        "$.delta.container.id: wrong type (expected string)"]


# ------------------------------------------------------------------------------------ nothing dead in the keep list
def _all_events() -> list[dict]:
    return [e["data"] for body in SAMPLES.values() for e in events(body)]


def _tag(value) -> str | None:
    return value.get("type") if isinstance(value, dict) else None


def test_every_kept_top_level_field_of_the_message_is_in_a_sample():
    missing = set(MESSAGE.fields) - {k for body in SAMPLES.values() for k in body}
    assert not missing, sorted(missing)


def test_every_kept_top_level_field_of_an_event_is_in_a_sample_stream():
    missing = set(STREAM.fields) - {k for data in _all_events() for k in data}
    assert not missing, sorted(missing)


def test_every_kept_block_of_the_message_is_in_a_sample():
    used = {b["type"] for body in SAMPLES.values() for b in body["content"]}
    assert sorted(set(MESSAGE.variants["content[]"]) - used) == []


@pytest.mark.parametrize("path", sorted(set(STREAM.variants) - set(NOT_EXERCISED)))
def test_every_kept_variant_of_the_stream_is_in_a_sample_stream(path):
    if path == "":
        used = {_tag(d) for d in _all_events()}
    else:
        used = {_tag(d.get(path)) for d in _all_events()}
    assert sorted(set(STREAM.variants[path]) - used) == []


def test_every_response_allowed_gap_is_in_a_sample_or_a_recording():
    """A gap is evidenced by a body that carries it where the contract puts it. A stream gap: an event of a sample's
    stream or of a committed recording (the provider's real stream; `recorded_exchanges.py`). A message gap: a sample
    (the recordings hold streams only). A gap nothing carries is dead: the provider no longer sends it."""
    recorded = [e["data"] for p, n in EXCHANGES if provider_of(p) == "anthropic"
                for e in recording(p)["exchanges"][n]["response"]["events"]]
    assert recorded, "no Anthropic recording: the recorded half of the evidence is missing"
    stream_events = [*_all_events(), *recorded]
    dead = [a for a in STREAM.allowed if not any(carries(a, d) for d in stream_events)]
    dead += [a for a in MESSAGE.allowed if not any(carries(a, body) for body in SAMPLES.values())]
    assert not dead, f"ALLOWED response gaps no sample or recording carries: {dead}"


def test_the_not_exercised_list_names_only_kept_variant_paths():
    assert set(NOT_EXERCISED) <= set(STREAM.variants)
