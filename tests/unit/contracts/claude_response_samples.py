"""Responses of `POST /v1/messages`, as the wire carries them, for the response-conformance meta-tests (P17).

`SAMPLES` are whole messages (the `application/json` shape, which is also what the SDK assembles from a stream);
`events(message)` renders one as the server-sent events of a stream (the shape on the wire: ClaudeAdapter always
streams), following Anthropic's streaming protocol: message_start with an empty content list, per block a
content_block_start, its deltas and a content_block_stop, a message_delta with the stop reason and the usage, then
message_stop; a ping after message_start.

`test_claude_response_conformance.py` checks both shapes against `contracts/providers/anthropic/openapi.yaml` and runs
the events through the REAL SDK and `ClaudeAdapter`. A sample is a test input that exercises the contract's kept fields
and variants; it is not a recording (recordings come from `make contract-record PROVIDER=anthropic` and live under
`tests/fixtures/providers/anthropic/`). Examples come from the invented universe (tests/CLAUDE.md "Invented cast").
"""
from __future__ import annotations

import copy
import json
from typing import Any

MODEL = "claude-haiku-5-5"
GROUNDED_MODEL = "claude-sonnet-5-5"
CONTAINER = {"id": "container_sample", "expires_at": "2026-11-19T12:00:00Z"}


def usage(input_tokens: int = 40, output_tokens: int = 12, *, cache_read: int = 0, cache_write: int = 0,
          searches: int = 0, fetches: int = 0, thinking: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write, "service_tier": "standard", "inference_geo": None,
        "cache_creation": {"ephemeral_5m_input_tokens": cache_write, "ephemeral_1h_input_tokens": 0},
        "server_tool_use": {"web_search_requests": searches, "web_fetch_requests": fetches},
    }
    if thinking is not None:
        out["output_tokens_details"] = {"thinking_tokens": thinking}
    return out


def message(content: list[dict[str, Any]], *, stop_reason: str = "end_turn", model: str = MODEL,
            use: dict[str, Any] | None = None, stop_details: dict[str, Any] | None = None,
            container: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "msg_sample", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details,
            "container": container, "usage": usage() if use is None else use}


def text(value: str, citations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "text", "text": value, "citations": citations}


def tool_use(name: str, args: dict[str, Any], block_id: str = "toolu_sample") -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": args}


def server_tool_use(name: str, args: dict[str, Any], block_id: str) -> dict[str, Any]:
    return {"type": "server_tool_use", "id": block_id, "name": name, "input": args}


_PORTO = "https://example.com/festivals/porto"
_SEARCH_RESULT = {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
    {"type": "web_search_result", "url": _PORTO, "title": "Porto festivals", "encrypted_content": "enc-1",
     "page_age": "June 2026"}]}
_CITED = text("Sao Joao is celebrated in Porto on 24 June.", citations=[
    {"type": "web_search_result_location", "url": _PORTO, "title": "Porto festivals", "encrypted_index": "idx-1",
     "cited_text": "Sao Joao, 24 June, Porto"}])

# The usage legs of the cache sample: 300 uncached input tokens, 4200 read from the cache, 900 written to it.
CACHE_USAGE = usage(300, 20, cache_read=4200, cache_write=900)

SAMPLES: dict[str, dict[str, Any]] = {
    "text": message([text("Alex Doe's 2015 Skoda Fabia is booked for its service on 2026-11-05.")]),
    "tool_use_after_thinking": message(
        [{"type": "thinking", "thinking": "The user asks for the date of the car service.", "signature": "sig-sample"},
         {"type": "redacted_thinking", "data": "opaque-sample"},
         text("Let me look that up."),
         tool_use("lookup_value", {"key": "car service"})],
        stop_reason="tool_use", use=usage(90, 40, thinking=22)),
    "grounded_with_citations": message(
        [server_tool_use("web_search", {"query": "Sao Joao festival Porto date"}, "srvtoolu_1"), _SEARCH_RESULT,
         _CITED],
        use=usage(2400, 60, searches=1)),
    "grounded_server_tools_paused": message(
        [server_tool_use("web_fetch", {"url": _PORTO}, "srvtoolu_2"),
         {"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_2", "content": {
             "type": "web_fetch_result", "url": _PORTO, "retrieved_at": "2026-11-19T10:00:00Z", "content": {
                 "type": "document", "title": "Porto festivals", "citations": {"enabled": True},
                 "source": {"type": "text", "media_type": "text/plain", "data": "Sao Joao, 24 June, Porto."}}}},
         server_tool_use("code_execution", {"code": "print(24)"}, "srvtoolu_3"),
         {"type": "code_execution_tool_result", "tool_use_id": "srvtoolu_3", "content": {
             "type": "code_execution_result", "stdout": "24\n", "stderr": "", "return_code": 0, "content": []}},
         server_tool_use("bash_code_execution", {"command": "ls"}, "srvtoolu_4"),
         {"type": "bash_code_execution_tool_result", "tool_use_id": "srvtoolu_4", "content": {
             "type": "bash_code_execution_result", "stdout": "notes.txt\n", "stderr": "", "return_code": 0,
             "content": []}},
         server_tool_use("text_editor_code_execution", {"command": "view", "path": "notes.txt"}, "srvtoolu_5"),
         {"type": "text_editor_code_execution_tool_result", "tool_use_id": "srvtoolu_5", "content": {
             "type": "text_editor_code_execution_view_result", "content": "24 June", "file_type": "text",
             "num_lines": 1, "start_line": 1, "total_lines": 1}}],
        stop_reason="pause_turn", model=GROUNDED_MODEL, use=usage(3100, 80, fetches=1), container=CONTAINER),
    "max_tokens": message([text("Clara Moreira's weekend in Coimbra is Mar")], stop_reason="max_tokens",
                          use=usage(30, 16)),
    "refusal": message([], stop_reason="refusal", use=usage(25, 0),
                       stop_details={"type": "refusal", "category": "general_harms", "explanation": "declined"}),
    "cache_read_and_write": message([text("Clara Moreira's weekend in Coimbra is Mar 6-8.")], use=CACHE_USAGE),
}

# The grounded loop's last call after a pause_turn: the turn ends with the cited answer.
GROUNDED_FINAL = message([_CITED], model=GROUNDED_MODEL, use=usage(3300, 40, searches=0), container=CONTAINER)


def _start_block(block: dict[str, Any]) -> dict[str, Any]:
    """A block as content_block_start carries it: text, thinking and tool input empty (filled by deltas)."""
    kind = block["type"]
    if kind == "text":
        return {"type": "text", "text": ""}
    if kind == "thinking":
        return {"type": "thinking", "thinking": "", "signature": ""}
    if kind in ("tool_use", "server_tool_use"):
        return {**block, "input": {}}
    return copy.deepcopy(block)


def _deltas(block: dict[str, Any]) -> list[dict[str, Any]]:
    kind = block["type"]
    if kind == "text":
        out = [{"type": "text_delta", "text": block["text"]}] if block["text"] else []
        return out + [{"type": "citations_delta", "citation": c} for c in block.get("citations") or []]
    if kind == "thinking":
        return [{"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]}]
    if kind in ("tool_use", "server_tool_use"):
        raw = json.dumps(block["input"])
        half = len(raw) // 2
        return [{"type": "input_json_delta", "partial_json": raw[:half]},
                {"type": "input_json_delta", "partial_json": raw[half:]}]
    return []


def events(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """The message as the server-sent events of a stream: `[{"event": name, "data": JSON}]` in wire order."""
    use = msg["usage"]
    start_usage = {**use, "output_tokens": 1}
    start = {**msg, "content": [], "stop_reason": None, "stop_sequence": None, "stop_details": None,
             "container": None, "usage": start_usage}
    out: list[dict[str, Any]] = [{"event": "message_start", "data": {"type": "message_start", "message": start}},
                                 {"event": "ping", "data": {"type": "ping"}}]
    for index, block in enumerate(msg["content"]):
        out.append({"event": "content_block_start",
                    "data": {"type": "content_block_start", "index": index, "content_block": _start_block(block)}})
        out += [{"event": "content_block_delta", "data": {"type": "content_block_delta", "index": index, "delta": d}}
                for d in _deltas(block)]
        out.append({"event": "content_block_stop", "data": {"type": "content_block_stop", "index": index}})
    delta: dict[str, Any] = {"stop_reason": msg["stop_reason"], "stop_sequence": None}
    if msg.get("stop_details"):
        delta["stop_details"] = msg["stop_details"]
    if msg.get("container"):
        delta["container"] = msg["container"]
    end_usage = {k: use[k] for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                     "cache_creation_input_tokens", "server_tool_use")}
    out.append({"event": "message_delta", "data": {"type": "message_delta", "delta": delta, "usage": end_usage}})
    out.append({"event": "message_stop", "data": {"type": "message_stop"}})
    return out


def sse(stream: list[dict[str, Any]]) -> bytes:
    """Events as the text/event-stream body the transport answers with."""
    return "".join(f"event: {e['event']}\ndata: {json.dumps(e['data'])}\n\n" for e in stream).encode("utf-8")
