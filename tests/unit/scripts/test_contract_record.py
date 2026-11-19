"""scripts/contracts/record.py: the recorder of real provider exchanges (P17), offline.

The provider is replaced by an `httpx.MockTransport` passed as the recorder's upstream (the real adapter and SDK still
run); the network guard of tests/unit refuses anything else. What is pinned: the plan (the scenarios, their models are
priced, their first requests conform to the request contract), the scrubbing, the money guard (the cap stops a call
before it is sent; a call with tokens and a $0 cost stops the run), the dry run (no network, no key) and the recording
document (`_meta` with its regeneration command, the exchanges, the cost legs).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from scripts.contracts import record as rec
from src.ports.llm_port import LLMResponse, MessagePart, ToolCall
from tests.contracts.openapi_contract import assert_request, assert_response, assert_stream
from tests.unit.contracts import claude_response_samples as cs
from tests.unit.contracts import openai_response_samples as os_
from tests.unit.contracts.claude_request_shapes import NOT_REACHABLE

KINDS = {"plain_text", "forced_tool_call", "thinking_on", "prompt_cache_write_then_read", "grounding_citations",
         "structured_output", "replay_thinking_and_tool_use"}
SECRET = "sk-test-not-a-real-key"


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_TIER_OVERRIDES", "CLAUDE_SMALL_MODEL",
                 "CLAUDE_PERFORMANCE_MODEL"):
        monkeypatch.delenv(name, raising=False)


class Upstream:
    """A scripted provider: answers each request with the next response, counts the calls."""

    def __init__(self, *responses: httpx.Response):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.responses.pop(0)
        return httpx.MockTransport(handle)


_ID_HEADERS = {"request-id": "req_sample_123", "x-request-id": "req_sample_456", "openai-organization": "org-sample",
               "openai-project": "proj_sample", "anthropic-organization-id": "org-sample-2", "cf-ray": "ray-sample",
               "set-cookie": "session=sample"}


def _openai_reply(body: dict) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "application/json", **_ID_HEADERS}, json=body)


def _claude_reply(message: dict) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream", **_ID_HEADERS},
                          content=cs.sse(cs.events(message)))


def _run(provider: str, only: str, upstream: Upstream, tmp_path: Path, max_usd: float = 1.0) -> int:
    scenarios = rec.plan(provider, only)
    return asyncio.run(rec.record(provider, scenarios, SECRET, rec.Meter(max_usd), upstream=upstream.transport(),
                                  out_dir=tmp_path))


def _doc(tmp_path: Path, provider: str, scenario: str) -> dict:
    return json.loads((tmp_path / provider / f"{scenario}.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------------ the plan
@pytest.mark.parametrize("provider", sorted(rec.SCENARIOS))
def test_every_provider_records_every_response_kind_with_unique_names(provider):
    names = [s.name for s in rec.plan(provider)]
    assert len(names) == len(set(names)) and KINDS <= set(names)


def test_the_anthropic_plan_records_the_request_side_not_reachable_entries_or_names_them_unreachable():
    purposes = " ".join(s.purpose for s in rec.plan("anthropic"))
    for entry in NOT_REACHABLE:
        assert f"NOT_REACHABLE {entry}" in purposes or entry in rec.UNREACHABLE["anthropic"], entry
    assert {"replay_server_tool_blocks", "grounding_dynamic_container"} <= {s.name for s in rec.plan("anthropic")}
    assert "same_provider_fallback" in rec.UNREACHABLE["anthropic"]


@pytest.mark.parametrize("provider", sorted(rec.SCENARIOS))
def test_every_scenario_model_is_priced_in_billing(provider):
    for scenario in rec.plan(provider):
        assert rec.calculate_cost(scenario.model, 1_000_000, 1_000_000) > 0, scenario.name


@pytest.mark.parametrize("provider, scenario", [(p, s.name) for p in sorted(rec.SCENARIOS) for s in rec.plan(p)])
def test_every_first_request_conforms_to_the_request_contract_before_any_money_is_spent(provider, scenario):
    sent = asyncio.run(rec.first_request(provider, next(s for s in rec.plan(provider) if s.name == scenario)))
    body = json.loads(sent.content)
    query: dict[str, list[str]] = {}
    for name, value in sent.url.params.multi_items():
        query.setdefault(name, []).append(value)
    # Method, path, query and headers as the SDK sent them, so a moved endpoint or an added query parameter fails here.
    assert_request(provider, sent.method, sent.url.path, dict(sent.headers), body, query=query)
    assert body.get("max_output_tokens" if provider == "openai" else "max_tokens") <= 2048   # small output caps


@pytest.mark.parametrize("provider", sorted(rec.SCENARIOS))
def test_the_cache_scenario_sends_the_same_long_prefix_twice(provider):
    scenario = rec.plan(provider, "prompt_cache_write_then_read")[0]
    first, second = (step([]) for step in scenario.steps)
    assert first.model_dump(exclude={"messages"}) == second.model_dump(exclude={"messages"})
    assert [m.parts for m in first.messages] == [m.parts for m in second.messages]
    assert first.system_instruction == rec.HANDBOOK and first.cache_config.enabled and len(rec.HANDBOOK) > 4096 * 4


def test_thinking_is_off_unless_the_scenario_needs_it():
    for provider in rec.SCENARIOS:
        for scenario in rec.plan(provider):
            request = scenario.steps[0]([])
            assert (request.thinking is not None) == (scenario.name in ("thinking_on", "replay_thinking_and_tool_use"))


def test_only_selects_one_scenario_and_an_unknown_one_is_refused():
    assert [s.name for s in rec.plan("openai", "plain_text")] == ["plain_text"]
    with pytest.raises(SystemExit, match="no scenario 'nope' for openai"):
        rec.plan("openai", "nope")


def test_a_replay_step_sends_the_previous_turn_back_with_the_tool_results():
    first = rec.plan("anthropic", "replay_thinking_and_tool_use")[0].steps[0]
    replay = rec.plan("anthropic", "replay_thinking_and_tool_use")[0].steps[1]
    raw = object()
    answer = LLMResponse(text="", tool_calls=[ToolCall(name="lookup_value", args={"key": "car service"}, thought_signature="t1")],
                         raw_content=raw)
    request = replay([answer])
    head, model_turn, results = request.messages[:-2], request.messages[-2], request.messages[-1]
    assert [m.parts for m in head] == [m.parts for m in first([]).messages] and model_turn.raw_content is raw
    assert results.parts[0].tool_response == {"name": "lookup_value", "response": "2026-11-05", "tool_use_id": "t1"}
    plain = replay([LLMResponse(text="Done.", raw_content=raw)])
    assert (plain.messages[-1].role, plain.messages[-1].parts) == ("user", [MessagePart(text="Thanks.")])


# ---------------------------------------------------------------------------------------------------- scrubbing
def test_scrub_keeps_only_the_allowlisted_headers():
    headers = {"Authorization": f"Bearer {SECRET}", "x-api-key": SECRET, "Content-Type": "application/json",
               "anthropic-beta": "prompt-caching-2024-07-31", "anthropic-version": "2023-06-01",
               "OpenAI-Organization": "org-sample", "OpenAI-Project": "proj_sample", "User-Agent": "sdk/1",
               "x-stainless-os": "MacOS"}
    assert rec.scrub_headers(headers, rec.REQUEST_HEADERS) == {
        "anthropic-beta": "prompt-caching-2024-07-31", "anthropic-version": "2023-06-01",
        "content-type": "application/json"}
    assert rec.scrub_headers({**_ID_HEADERS, "Content-Type": "text/event-stream"}, rec.RESPONSE_HEADERS) == {
        "content-type": "text/event-stream"}


@pytest.mark.parametrize("provider, reply", [("openai", _openai_reply(os_.SAMPLES["text"])),
                                             ("anthropic", _claude_reply(cs.SAMPLES["text"]))])
def test_a_recording_holds_no_key_and_no_id_header(provider, reply, tmp_path):
    assert _run(provider, "plain_text", Upstream(reply), tmp_path) == 0
    text = (tmp_path / provider / "plain_text.json").read_text(encoding="utf-8")
    assert SECRET not in text
    for name, value in _ID_HEADERS.items():
        assert name not in text and value not in text, name
    doc = json.loads(text)
    for exchange in doc["exchanges"]:
        assert set(exchange["request"]["headers"]) <= rec.REQUEST_HEADERS
        assert set(exchange["response"]["headers"]) <= rec.RESPONSE_HEADERS


# ------------------------------------------------------------------------------------------------- the recording
def test_an_openai_recording_keeps_the_exchange_its_cost_legs_and_its_regeneration_command(tmp_path, capsys):
    upstream = Upstream(_openai_reply(os_.SAMPLES["cache_read_and_write"]))
    assert _run("openai", "plain_text", upstream, tmp_path) == 0
    doc = _doc(tmp_path, "openai", "plain_text")
    meta = doc["_meta"]
    assert meta["source"] == "recorded" and meta["outcome"] == "ok" and meta["model"] == "gpt-6-luna"
    assert meta["regenerate"] == "make contract-record PROVIDER=openai ONLY=plain_text"
    assert meta["operation"] == "POST /responses" and meta["sdk"].startswith("openai ")
    (exchange,) = doc["exchanges"]
    assert exchange["request"]["body"] == json.loads(upstream.requests[0].content)
    assert exchange["response"]["body"] == os_.SAMPLES["cache_read_and_write"]
    usage = os_.CACHE_USAGE
    cached, written = usage["input_tokens_details"]["cached_tokens"], usage["input_tokens_details"]["cache_write_tokens"]
    assert exchange["cost"] == {
        "input": usage["input_tokens"] - cached - written, "cache_read": cached, "cache_write": written,
        "output": usage["output_tokens"], "searches": 0,
        "usd": rec.calculate_cost("gpt-6-luna", usage["input_tokens"] - cached - written, usage["output_tokens"],
                                  cached, written)}
    assert meta["cost_usd"] == exchange["cost"]["usd"] > 0
    assert "input=100 cache_read=600 cache_write=300 output=20" in capsys.readouterr().out
    assert_response("openai", "POST", "/v1/responses", 200, exchange["response"]["body"])


def test_an_anthropic_recording_keeps_the_stream_as_events(tmp_path):
    upstream = Upstream(_claude_reply(cs.SAMPLES["cache_read_and_write"]))
    assert _run("anthropic", "plain_text", upstream, tmp_path) == 0
    (exchange,) = _doc(tmp_path, "anthropic", "plain_text")["exchanges"]
    assert exchange["response"]["events"] == cs.events(cs.SAMPLES["cache_read_and_write"])
    assert exchange["cost"]["input"] == cs.CACHE_USAGE["input_tokens"]
    assert (exchange["cost"]["cache_read"], exchange["cost"]["cache_write"]) == (
        cs.CACHE_USAGE["cache_read_input_tokens"], cs.CACHE_USAGE["cache_creation_input_tokens"])
    assert_stream("anthropic", "POST", "/v1/messages", 200, exchange["response"]["events"])


def test_a_two_step_scenario_records_both_calls_in_order(tmp_path):
    upstream = Upstream(_openai_reply(os_.SAMPLES["function_call_after_reasoning"]),
                        _openai_reply(os_.SAMPLES["text"]))
    assert _run("openai", "replay_thinking_and_tool_use", upstream, tmp_path) == 0
    first, second = _doc(tmp_path, "openai", "replay_thinking_and_tool_use")["exchanges"]
    replayed = [item.get("type") for item in second["request"]["body"]["input"]]
    assert "function_call" in replayed and "function_call_output" in replayed and "reasoning" in replayed
    assert first["response"]["body"]["output"][1]["type"] == "function_call"


def test_a_provider_error_ends_the_scenario_and_is_its_recorded_outcome(tmp_path):
    error = httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                 "message": "caller: null is not allowed"}})
    assert _run("anthropic", "replay_thinking_and_tool_use", Upstream(_claude_reply(
        cs.SAMPLES["tool_use_after_thinking"]), error), tmp_path) == 1
    doc = _doc(tmp_path, "anthropic", "replay_thinking_and_tool_use")
    assert doc["_meta"]["outcome"].startswith("LLMClientError") and len(doc["exchanges"]) == 2
    assert doc["exchanges"][1]["response"]["status"] == 400


# ------------------------------------------------------------------------------------------------------- money
def test_the_cap_stops_a_call_before_it_is_sent_and_writes_nothing(tmp_path, capsys):
    upstream = Upstream(_openai_reply(os_.SAMPLES["text"]))
    assert _run("openai", "plain_text", upstream, tmp_path, max_usd=0.0000001) == 4
    assert upstream.requests == [] and not (tmp_path / "openai").exists()
    assert "STOP (budget)" in capsys.readouterr().out


def test_the_cap_counts_what_was_spent(tmp_path):
    meter = rec.Meter(max_usd=1.0, spent=0.9999)
    with pytest.raises(rec.BudgetExceeded, match="spent \\$0.9999"):
        meter.before({"model": "gpt-6-luna", "max_output_tokens": 300, "input": "x"})
    assert rec.Meter(max_usd=1.0).before({"model": "gpt-6-luna", "max_output_tokens": 300}) > 0


def test_the_estimate_is_pessimistic_about_grounding_and_a_missing_output_cap():
    plain = {"model": "claude-haiku-5-5", "max_tokens": 100, "messages": []}
    grounded = {**plain, "tools": [{"type": "web_search_20250305", "name": "web_search"}]}
    uncapped = {"model": "claude-haiku-5-5", "messages": []}
    assert rec.estimate_call(grounded) >= rec.estimate_call(plain) + rec.SEARCHES_PER_CALL_ALLOWANCE * rec.SEARCH_USD
    assert rec.estimate_call(uncapped) > rec.estimate_call(plain)
    size = len(json.dumps(plain).encode())
    assert rec.estimate_call(plain) >= rec.calculate_cost("claude-haiku-5-5", size, 100)


def test_an_unpriced_model_is_refused_before_the_call(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "calculate_cost", lambda *a, **k: 0.0)
    upstream = Upstream(_openai_reply(os_.SAMPLES["text"]))
    assert _run("openai", "plain_text", upstream, tmp_path) == 3
    assert upstream.requests == []


def test_a_call_with_tokens_and_a_zero_cost_stops_the_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rec.Meter, "before", lambda self, body: 0.0)
    monkeypatch.setattr(rec, "calculate_cost", lambda *a, **k: 0.0)
    upstream = Upstream(_openai_reply(os_.SAMPLES["text"]))
    assert _run("openai", "plain_text", upstream, tmp_path) == 3
    assert len(upstream.requests) == 1 and not (tmp_path / "openai").exists()
    assert "STOP (broken meter)" in capsys.readouterr().out


def test_a_zero_cost_without_tokens_is_not_a_broken_meter():
    cost = rec.Meter(1.0).after("openai", "gpt-6-luna", {"body": {"usage": {"input_tokens": 0, "output_tokens": 0}}})
    assert cost["usd"] == 0


def test_usage_legs_of_openai_are_disjoint_and_count_searches():
    legs = rec.usage_legs("openai", {"body": os_.SAMPLES["grounded_with_citations"]})
    assert legs.searches == 1  # one search action; open_page and find_in_page are not searches
    cache = rec.usage_legs("openai", {"body": os_.SAMPLES["cache_read_and_write"]})
    assert (cache.input, cache.cache_read, cache.cache_write) == (100, 600, 300)


def test_usage_legs_of_a_stream_merge_message_start_and_message_delta():
    legs = rec.usage_legs("anthropic", {"events": cs.events(cs.SAMPLES["grounded_with_citations"])})
    final = cs.SAMPLES["grounded_with_citations"]["usage"]
    assert (legs.input, legs.output, legs.searches) == (final["input_tokens"], final["output_tokens"], 1)
    assert rec.usage_legs("anthropic", {"body": cs.SAMPLES["text"]}).output == cs.SAMPLES["text"]["usage"]["output_tokens"]


# ------------------------------------------------------------------------------------------------------ dry run
def test_the_dry_run_touches_no_network_and_needs_no_key(monkeypatch, capsys):
    async def refuse(self, request):
        raise AssertionError(f"network call in a dry run: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    assert rec.main(["--provider", "anthropic", "--dry-run"]) == 0
    out = capsys.readouterr().out
    for scenario in rec.plan("anthropic"):
        assert scenario.name in out
    assert "same_provider_fallback" in out and "not recorded" in out and "pessimistic total $" in out


def test_without_a_key_the_recorder_refuses_to_start(tmp_path):
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY is not set"):
        rec.main(["--provider", "anthropic", "--only", "plain_text"], upstream=Upstream().transport(), out_dir=tmp_path)


def test_the_env_file_supplies_the_key_and_the_environment_wins(tmp_path, monkeypatch):
    env = tmp_path / "keys.env"
    env.write_text(f"OPENAI_API_KEY={SECRET}\n", encoding="utf-8")
    upstream = Upstream(_openai_reply(os_.SAMPLES["text"]))
    assert rec.main(["--provider", "openai", "--only", "plain_text", "--env-file", str(env)],
                    upstream=upstream.transport(), out_dir=tmp_path) == 0
    assert upstream.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-environment")
    upstream = Upstream(_openai_reply(os_.SAMPLES["text"]))
    rec.main(["--provider", "openai", "--only", "plain_text", "--env-file", str(env)], upstream=upstream.transport(),
             out_dir=tmp_path)
    assert upstream.requests[0].headers["authorization"] == "Bearer sk-from-environment"
    with pytest.raises(SystemExit, match="no such file"):
        rec.main(["--provider", "openai", "--env-file", str(tmp_path / "missing.env")])


# ------------------------------------------------------------------------------------------------------ SSE parsing
def test_parse_sse_reads_names_json_multiline_and_plain_data():
    text = "event: a\r\ndata: {\"type\": \"a\"}\r\n\r\nevent: b\ndata: line one\ndata: line two\n\n: comment\n\n"
    assert rec.parse_sse(text) == [{"event": "a", "data": {"type": "a"}},
                                   {"event": "b", "data": "line one\nline two"}]
