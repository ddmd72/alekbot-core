"""Every committed recording replayed through the REAL adapter and SDK (P17).

The scenario's own steps (`record.SCENARIOS`, the request builders the recorder used) run again through the adapter the
recorder builds (`record.build_adapter`), its SDK's transport answering exchange N with recording N's response (a stream
re-rendered by `claude_response_samples.sse`). The recordings are discovered from the folder (`recorded_exchanges.py`).
- The adapter's requests must equal the recorded ones (body, path, kept headers; no query, which the recorder would
  not keep): an adapter change that alters the wire fails here, and the message names the regeneration command.
- The `LLMResponse` the adapter builds is compared with values derived from the recording, never with literals: text
  (OpenAI with the sources footer the adapter appends from the url_citation annotations), tool calls (name, arguments,
  id as `thought_signature`), finish reason, usage legs by each adapter's formula, thinking blocks, citations.
- The scenarios with a purpose of their own assert it: the cache scenario's second call reads what the first wrote; a
  replay scenario sends the first answer back as it came, with the tool results answering its calls.
The format and the contracts of the same recordings: `test_recorded_exchanges.py`.
"""
from __future__ import annotations

import asyncio
import functools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.contracts import record as rec
from src.adapters.claude_adapter import ClaudeAdapter
from src.domain.exceptions import LLMError
from src.domain.llm import FinishReason
from src.ports.llm_port import LLMRequest, LLMResponse
from tests.unit.contracts.claude_response_samples import sse
from tests.unit.contracts.recorded_exchanges import (
    RECORDINGS,
    PreconditionFailed,
    provider_of,
    recording,
    require,
)
from tests.unit.contracts.recorded_exchanges import recording_id as _id

# What each provider's wire value means for the adapter's finish reason (Claude stop_reason; OpenAI incomplete reason).
CLAUDE_STOP = {"end_turn": FinishReason.STOP, "stop_sequence": FinishReason.STOP, "tool_use": FinishReason.STOP,
               "max_tokens": FinishReason.MAX_TOKENS, "model_context_window_exceeded": FinishReason.MAX_TOKENS}
OPENAI_INCOMPLETE = {"max_output_tokens": FinishReason.MAX_TOKENS, "content_filter": FinishReason.SAFETY}


@pytest.fixture(autouse=True)
def _no_model_overrides(monkeypatch):
    for name in ("OPENAI_TIER_OVERRIDES", "CLAUDE_PERFORMANCE_MODEL", "CLAUDE_SMALL_MODEL", "ANTHROPIC_LOG"):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------------------------------- the replay: the wire
@dataclass
class Replay:
    """One scenario run again: what the adapter sent, which step sent it, and what it built."""

    sent: list[httpx.Request] = field(default_factory=list)
    step_of: list[int] = field(default_factory=list)
    requests: list[LLMRequest] = field(default_factory=list)
    responses: list[LLMResponse] = field(default_factory=list)
    error: LLMError | None = None


def _wire_response(recorded: dict[str, Any]) -> httpx.Response:
    status, headers = recorded["status"], recorded["headers"]
    if "events" in recorded:
        return httpx.Response(status, headers=headers, content=sse(recorded["events"]))
    if "body" in recorded:
        return httpx.Response(status, headers=headers, content=json.dumps(recorded["body"]).encode("utf-8"))
    return httpx.Response(status, headers=headers, text=recorded["text"])


@functools.lru_cache(maxsize=None)
def replay(path: Path) -> Replay:
    doc = recording(path)
    provider, exchanges = doc["_meta"]["provider"], doc["exchanges"]
    scenario = rec.SCENARIOS[provider]()[doc["_meta"]["scenario"]]
    out = Replay()

    def answer(request: httpx.Request) -> httpx.Response:
        out.sent.append(request)
        out.step_of.append(len(out.requests) - 1)
        if len(out.sent) > len(exchanges):
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                       "message": "no recorded exchange left"}})
        return _wire_response(exchanges[len(out.sent) - 1]["response"])

    async def run() -> None:
        adapter = rec.build_adapter(provider, "replay", httpx.MockTransport(answer))
        try:
            for step in scenario.steps:
                out.requests.append(step(out.responses))
                try:
                    out.responses.append(await adapter.generate_content(out.requests[-1]))
                except LLMError as error:
                    out.error = error
                    break
        finally:
            await adapter.client.close()

    asyncio.run(run())
    return out


@pytest.mark.parametrize("path", RECORDINGS, ids=_id)
def test_the_adapter_sends_the_recorded_requests(path):
    doc, run = recording(path), replay(path)
    regenerate = (f"{_id(path)}: the adapter's request differs from the recording; if the change is intended, "
                  f"regenerate it: {doc['_meta']['regenerate']}")
    assert len(run.sent) == len(doc["exchanges"]), regenerate
    for n, (sent, exchange) in enumerate(zip(run.sent, doc["exchanges"])):
        recorded = exchange["request"]
        assert sent.url.query == b"", f"exchange {n}: the SDK sent a query, which the recorder does not keep"
        assert (sent.method, sent.url.path) == (recorded["method"], recorded["path"]), f"exchange {n}: {regenerate}"
        kept = rec.scrub_headers(sent.headers, rec.REQUEST_HEADERS)
        assert kept == recorded["headers"], f"exchange {n}: {regenerate}"
        assert json.loads(sent.content) == recorded["body"], f"exchange {n}: {regenerate}"


@pytest.mark.parametrize("path", RECORDINGS, ids=_id)
def test_the_replay_ends_as_the_recording_did(path):
    outcome, run = recording(path)["_meta"]["outcome"], replay(path)
    if outcome == "ok":
        assert run.error is None and len(run.responses) == len(run.requests)
    else:
        assert run.error is not None and outcome.startswith(f"{type(run.error).__name__}:")


# --------------------------------------------------------------------------------- the replay: what the adapter read
def _blocks(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The content blocks of one Claude stream, assembled from its events as the streaming protocol defines them."""
    blocks: dict[int, dict[str, Any]] = {}
    partial: dict[int, str] = {}
    for event in events:
        data = event["data"]
        if data.get("type") == "content_block_start":
            blocks[data["index"]] = dict(data["content_block"])
        elif data.get("type") == "content_block_delta":
            block, delta = blocks[data["index"]], data["delta"]
            if delta["type"] == "text_delta":
                block["text"] += delta["text"]
            elif delta["type"] == "thinking_delta":
                block["thinking"] += delta["thinking"]
            elif delta["type"] == "signature_delta":
                block["signature"] = delta["signature"]
            elif delta["type"] == "input_json_delta":
                partial[data["index"]] = partial.get(data["index"], "") + delta["partial_json"]
            elif delta["type"] == "citations_delta":
                block["citations"] = [*(block.get("citations") or []), delta["citation"]]
            else:
                raise PreconditionFailed(f"a delta this derivation does not know: {delta['type']}")
    for index, text in partial.items():
        if text:
            blocks[index]["input"] = json.loads(text)
    return [blocks[i] for i in sorted(blocks)]


def _claude_usage(events: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
    """The final usage (message_start's, updated by message_delta's non-null values) and the stop reason."""
    usage: dict[str, Any] = {}
    stop = None
    for event in events:
        data = event["data"]
        if data.get("type") == "message_start":
            usage.update(data["message"]["usage"])
        elif data.get("type") == "message_delta":
            usage.update({k: v for k, v in (data.get("usage") or {}).items() if v is not None})
            stop = data["delta"].get("stop_reason")
    return usage, stop


def _legs(provider: str, response: dict[str, Any]) -> tuple[int, int, int, int]:
    """(input, cache read, cache write, output) by the adapter's formula: Claude's legs as reported; OpenAI's input
    includes both cache legs, so the three are made disjoint."""
    if provider == "anthropic":
        usage, _ = _claude_usage(response["events"])
        return (usage["input_tokens"], usage.get("cache_read_input_tokens") or 0,
                usage.get("cache_creation_input_tokens") or 0, usage["output_tokens"])
    usage = response["body"]["usage"]
    details = usage.get("input_tokens_details") or {}
    cached, written = details.get("cached_tokens") or 0, details.get("cache_write_tokens") or 0
    return max(usage["input_tokens"] - cached - written, 0), cached, written, usage["output_tokens"]


def _usage(response: LLMResponse) -> tuple[int, int, int, int, int]:
    u = response.usage_metadata
    assert u is not None
    return u.prompt_tokens, u.cache_read_tokens, u.cache_creation_tokens, u.completion_tokens, u.total_tokens


def _with_total(legs: tuple[int, int, int, int], total_input: int) -> tuple[int, int, int, int, int]:
    return (*legs, total_input + legs[3])


def _check_claude(request: LLMRequest, response: LLMResponse, exchanges: list[dict[str, Any]]) -> None:
    require(not (isinstance(request.response_schema, dict) and request.tools and not request.use_grounding),
            "the deliver_response path is not derived here: extend the test for that recording")
    blocks = [b for ex in exchanges for b in _blocks(ex["response"]["events"])]
    assert response.text == "".join(b["text"] for b in blocks if b["type"] == "text")
    assert [(c.name, c.args, c.thought_signature) for c in response.tool_calls] == [
        (b["name"], b["input"], b["id"]) for b in blocks if b["type"] == "tool_use"]
    legs = [_legs("anthropic", ex["response"]) for ex in exchanges]
    summed = tuple(sum(leg[i] for leg in legs) for i in range(4))
    assert _usage(response) == _with_total(summed, summed[0])   # Claude's total: input + output
    if request.use_grounding and any(m in request.model_name for m in ClaudeAdapter._DYNAMIC_SEARCH_MODELS):
        # The grounded pause_turn loop: no finish reason, no raw_content, usage summed over its calls.
        assert response.finish_reason is None and response.raw_content is None
        return
    require(len(exchanges) == 1, "one call per step off the grounded loop")
    assert response.finish_reason == CLAUDE_STOP[_claude_usage(exchanges[0]["response"]["events"])[1]]
    raw = response.raw_content.content
    assert [b.type for b in raw] == [b["type"] for b in blocks]
    for got, want in zip(raw, blocks):
        if want["type"] == "thinking":
            assert (got.thinking, got.signature) == (want["thinking"], want["signature"])
        if want["type"] == "text":
            assert [(getattr(c, "url", None), c.cited_text) for c in got.citations or []] == [
                (c.get("url"), c["cited_text"]) for c in want.get("citations") or []]


def _openai_text(body: dict[str, Any]) -> str:
    """The output text, then the sources footer the adapter appends from the url_citation annotations (deduplicated)."""
    parts, sources = [], []
    for item in body["output"]:
        if item["type"] != "message":
            continue
        for part in item["content"]:
            if part["type"] == "output_text":
                parts.append(part["text"])
                sources += [f"- [{a['title']}]({a['url']})" if a.get("title") else f"- {a['url']}"
                            for a in part.get("annotations") or [] if a["type"] == "url_citation" and a.get("url")]
    text = "".join(parts)
    unique = list(dict.fromkeys(sources))
    return text + ("\n\n*Sources:*\n" + "\n".join(unique) if unique else "")


def _check_openai(response: LLMResponse, exchanges: list[dict[str, Any]]) -> None:
    require(len(exchanges) == 1, "one call per step")
    body = exchanges[0]["response"]["body"]
    assert response.text == _openai_text(body)
    assert [(c.name, c.args, c.thought_signature) for c in response.tool_calls] == [
        (i["name"], json.loads(i["arguments"]) if i["arguments"] else {}, i["call_id"])
        for i in body["output"] if i["type"] == "function_call"]
    expected = (FinishReason.STOP if body["status"] == "completed" else
                OPENAI_INCOMPLETE.get((body.get("incomplete_details") or {}).get("reason"), FinishReason.OTHER)
                if body["status"] == "incomplete" else None)
    assert response.finish_reason == expected
    assert _usage(response) == _with_total(_legs("openai", exchanges[0]["response"]), body["usage"]["input_tokens"])
    assert [i.type for i in response.raw_content] == [i["type"] for i in body["output"]]   # reasoning items kept


@pytest.mark.parametrize("path", RECORDINGS, ids=_id)
def test_the_adapter_reads_what_the_recording_answered(path):
    doc, run = recording(path), replay(path)
    require(len(run.sent) == len(doc["exchanges"]), "the replay used every recorded exchange")
    for step, (request, response) in enumerate(zip(run.requests, run.responses)):
        exchanges = [ex for ex, s in zip(doc["exchanges"], run.step_of) if s == step]
        if provider_of(path) == "anthropic":
            _check_claude(request, response, exchanges)
        else:
            _check_openai(response, exchanges)


# ------------------------------------------------------------------------------- the replay: what scenarios show
@pytest.mark.parametrize("provider", sorted(rec.SCENARIOS))
def test_the_cache_scenario_reads_what_its_first_call_wrote(provider):
    path = rec.FIXTURES_DIR / provider / "prompt_cache_write_then_read.json"
    first, second = (_legs(provider, ex["response"]) for ex in recording(path)["exchanges"])
    assert first[2] > 0 and second[1] == first[2]   # the second call read what the first wrote
    written, read = replay(path).responses
    assert (written.usage_metadata.cache_creation_tokens, read.usage_metadata.cache_read_tokens) == (first[2], first[2])


REPLAYS = [p for p in RECORDINGS if p.stem.startswith("replay_")]


@pytest.mark.parametrize("path", REPLAYS, ids=_id)
def test_a_replay_scenario_sends_the_first_answer_back_as_it_came(path):
    first, second = recording(path)["exchanges"][:2]
    if provider_of(path) == "anthropic":
        blocks = _blocks(first["response"]["events"])
        turn = [m for m in second["request"]["body"]["messages"] if m["role"] == "assistant"][-1]["content"]
        assert [(b["type"], b.get("id")) for b in turn] == [(b["type"], b.get("id")) for b in blocks]
        answered = {b["tool_use_id"] for m in second["request"]["body"]["messages"] if m["role"] == "user"
                    for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"}
        assert answered == {b["id"] for b in blocks if b["type"] == "tool_use"}
    else:
        output = first["response"]["body"]["output"]
        sent = {i["id"]: i for i in second["request"]["body"]["input"] if "id" in i}
        assert [sent.get(i["id"]) for i in output] == output
        answered = {i["call_id"] for i in second["request"]["body"]["input"] if i.get("type") == "function_call_output"}
        assert answered == {i["call_id"] for i in output if i["type"] == "function_call"}
