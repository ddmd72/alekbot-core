"""Response bodies of `POST /responses`, as the wire carries them, for the response-conformance meta-tests (P17).

`test_openai_response_conformance.py` checks each against the response side of `contracts/providers/openai/openapi.yaml`
and runs it through the REAL SDK and `OpenAIAdapter` (the transport answers with it). A sample is a test input that
exercises the contract's kept fields and variants; it is not a recording (recordings come from
`make contract-record PROVIDER=openai` and live under `tests/fixtures/providers/openai/`). Examples come from the
invented universe (tests/CLAUDE.md "Invented cast").
"""
from __future__ import annotations

import json
from typing import Any

MODEL = "gpt-6-luna"


def _usage(input_tokens: int = 40, output_tokens: int = 12, *, cached: int = 0, written: int = 0,
           reasoning: int = 0) -> dict[str, Any]:
    details: dict[str, Any] = {"cached_tokens": cached, "cache_write_tokens": written}
    if written:
        details["cache_write_tokens_details"] = {"ttl_30m_tokens": written, "ttl_12h_tokens": 0}
    return {"input_tokens": input_tokens, "input_tokens_details": details, "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning}, "total_tokens": input_tokens + output_tokens}


def _response(output: list[dict[str, Any]], *, status: str = "completed", usage: dict[str, Any] | None = None,
              incomplete: dict[str, Any] | None = None, model: str = MODEL) -> dict[str, Any]:
    """A whole response object: the fields the adapter reads plus the provider's other required ones."""
    return {
        "id": "resp_sample", "object": "response", "created_at": 1763560000, "status": status,
        "access_programs": None, "error": None, "incomplete_details": incomplete, "instructions": None,
        "max_output_tokens": None, "model": model, "output": output, "parallel_tool_calls": True,
        "previous_response_id": None, "reasoning": {"effort": "none", "summary": None},
        "temperature": 1.0, "text": {"format": {"type": "text"}}, "tool_choice": "auto", "tools": [], "top_p": 1.0,
        "truncation": "disabled", "metadata": {}, "usage": _usage() if usage is None else usage, "user": None,
    }


def _message(text: str, *, annotations: list[dict[str, Any]] | None = None, status: str = "completed",
             phase: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": "msg_sample", "type": "message", "role": "assistant", "status": status,
        "content": [{"type": "output_text", "text": text, "annotations": annotations or [], "logprobs": []}],
    }
    if phase:
        item["phase"] = phase
    return item


def _reasoning(summary: str, content: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"id": "rs_sample", "type": "reasoning", "status": "completed", "encrypted_content": None,
                            "summary": [{"type": "summary_text", "text": summary}]}
    if content is not None:
        item["content"] = [{"type": "reasoning_text", "text": content}]
    return item


def _function_call(name: str, arguments: str, call_id: str = "call_sample", status: str = "completed") -> dict:
    return {"id": "fc_sample", "type": "function_call", "call_id": call_id, "name": name, "arguments": arguments,
            "status": status}


_SERVICE = "Alex Doe's 2015 Skoda Fabia is booked for its service on 2026-11-05."

# The usage legs of the cache sample: 1000 input tokens, 600 read from the cache, 300 written to it.
CACHE_USAGE = _usage(1000, 20, cached=600, written=300)

SAMPLES: dict[str, dict[str, Any]] = {
    "text": _response([_message(_SERVICE, phase="final_answer")]),
    "function_call_after_reasoning": _response(
        [_reasoning("Look up the service date.", "The user asks about the car service."),
         _function_call("lookup_value", json.dumps({"key": "car service"}))],
        usage=_usage(80, 30, reasoning=18)),
    "grounded_with_citations": _response(
        [_reasoning("Search the festival date."),
         {"id": "ws_1", "type": "web_search_call", "status": "completed",
          "action": {"type": "search", "queries": ["Sao Joao festival Porto date"],
                     "sources": [{"type": "url", "url": "https://example.com/festivals"}]}},
         {"id": "ws_2", "type": "web_search_call", "status": "completed",
          "action": {"type": "open_page", "url": "https://example.com/festivals/porto"}},
         {"id": "ws_3", "type": "web_search_call", "status": "completed",
          "action": {"type": "find_in_page", "pattern": "24 June", "url": "https://example.com/festivals/porto"}},
         _message("Sao Joao is celebrated in Porto on 24 June.", annotations=[
             {"type": "url_citation", "url": "https://example.com/festivals/porto", "title": "Porto festivals",
              "start_index": 0, "end_index": 43}])]),
    "incomplete_truncated_call": _response(
        [_function_call("lookup_value", '{"key": "Iron Te', status="incomplete")],
        status="incomplete", incomplete={"reason": "max_output_tokens"}, usage=_usage(50, 16)),
    "cache_read_and_write": _response([_message("Clara Moreira's weekend in Coimbra is Mar 6-8.")],
                                      usage=CACHE_USAGE),
}
