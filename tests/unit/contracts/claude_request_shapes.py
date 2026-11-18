"""The request shapes ClaudeAdapter builds, as `LLMRequest`s, for the request-conformance test (P17).

`test_claude_request_conformance.py` runs each shape through the REAL `ClaudeAdapter` and the REAL Anthropic SDK with
the HTTP transport replaced, and checks the request that leaves against `contracts/providers/anthropic/openapi.yaml`.
A shape is an input only: what it must produce is the contract, not an expected body written here.

Every field value is generic and technical. Every kept field and union variant of the Anthropic operation in
`scripts/contracts/extract_subset.py` is reached by at least one shape, except the ones in `NOT_REACHABLE`.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from src.domain.llm import USER_TURN_SYSTEM_ANCHOR
from src.ports.llm_port import (
    LLMRequest,
    Message,
    MessagePart,
    PROMPT_CACHE_BOUNDARY,
    PromptCacheConfig,
    ToolCall,
)

# The models the adapter names: its tiers (MODEL_TIERS), the fallback chain (_MODEL_FALLBACK), the 4.x generation it
# still serves through CLAUDE_SMALL_MODEL / CLAUDE_PERFORMANCE_MODEL, and the xhigh clamp list (_NO_XHIGH_MODELS).
MODELS = (
    "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2",
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5-20251001",
)
EFFORTS = (None, "low", "medium", "high", "xhigh")

# Keep-list entries of the Anthropic operation that no shape can reach, with the reason. The meta-test fails when an
# entry here is no longer in the keep list (stale) or when a shape reaches it (no longer needed).
NOT_REACHABLE: dict[str, str] = {
    "container": "sent only on a pause_turn continuation of the grounded loop, which needs a streamed provider response "
                 "(a stream fixture; not in this slice)",
}


@dataclass(frozen=True)
class Shape:
    """One request: the model, the `LLMRequest`, and whether the first answer is a 529 (the same-provider model
    fallback then sends a second request, also checked)."""

    request: LLMRequest
    fallback: bool = False


def _user(text: str) -> Message:
    return Message(role="user", parts=[MessagePart(text=text)])


def _model(text: str) -> Message:
    return Message(role="model", parts=[MessagePart(text=text)])


def _file(mime: str) -> dict[str, Any]:
    return {"base64": base64.b64encode(b"generic bytes").decode("ascii"), "mime_type": mime}


def _tool(name: str = "lookup") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Look an item up by key.",
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "the item key"}},
            "required": ["key"],
        },
    }


# A response schema with a nullable field and keywords the strict grammar compiler rejects (_make_strict drops them).
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "maxLength": 500},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "note": {"type": ["string", "null"]},
        "items": {"type": "array", "maxItems": 5, "items": {"type": "object", "properties": {"id": {"type": "string"}}}},
    },
    "required": ["answer"],
}

# A persona prompt with two sections: the adapter then adds the persona anchor next to the turn anchor.
PERSONA = "identity {\n  role: reviewer\n}\nvoice {\n  tone: plain\n}\nStatic rules.\n"


def _tool_turn(call_id: str = "toolu_01") -> list[Message]:
    call = ToolCall(name="lookup", args={"key": "k1"}, thought_signature=call_id)
    return [
        _user("Find k1."),
        Message(role="model", parts=[MessagePart(tool_call=call)]),
        Message(role="user", parts=[MessagePart(tool_response={
            "name": "lookup", "response": {"value": 1}, "tool_use_id": call_id})]),
    ]


def _raw_content_blocks() -> Any:
    """An assistant turn as the SDK parsed it, replayed unchanged (`Message.raw_content`: the adapter passes
    `raw_content.content` through, claude_adapter._convert_messages). Built offline from the SDK's response types."""
    from anthropic import types as t
    return t.Message(
        id="msg_01", type="message", role="assistant", model="claude-sonnet-5-5", stop_reason="end_turn",
        usage=t.Usage(input_tokens=1, output_tokens=1),
        content=[
            t.ThinkingBlock(type="thinking", thinking="The key exists.", signature="sig"),
            t.RedactedThinkingBlock(type="redacted_thinking", data="opaque"),
            t.TextBlock(type="text", text="Checking."),
            t.ToolUseBlock(type="tool_use", id="toolu_r1", name="lookup", input={"key": "k1"}),
            t.ServerToolUseBlock(type="server_tool_use", id="srvtoolu_1", name="web_search", input={"query": "build log"}),
            t.WebSearchToolResultBlock(type="web_search_tool_result", tool_use_id="srvtoolu_1", content=[
                t.WebSearchResultBlock(type="web_search_result", encrypted_content="enc", title="Build logs",
                                       url="https://example.com/logs")]),
            t.WebFetchToolResultBlock(type="web_fetch_tool_result", tool_use_id="srvtoolu_2", content=t.WebFetchBlock(
                type="web_fetch_result", url="https://example.com/logs", content=t.DocumentBlock(
                    type="document", title="Logs", source=t.PlainTextSource(
                        type="text", media_type="text/plain", data="log text")))),
            t.CodeExecutionToolResultBlock(type="code_execution_tool_result", tool_use_id="srvtoolu_3",
                                           content=t.CodeExecutionResultBlock(
                                               type="code_execution_result", stdout="1", stderr="", return_code=0, content=[])),
            t.BashCodeExecutionToolResultBlock(type="bash_code_execution_tool_result", tool_use_id="srvtoolu_4",
                                               content=t.BashCodeExecutionResultBlock(
                                                   type="bash_code_execution_result", stdout="ok", stderr="",
                                                   return_code=0, content=[])),
            t.TextEditorCodeExecutionToolResultBlock(
                type="text_editor_code_execution_tool_result", tool_use_id="srvtoolu_5",
                content=t.TextEditorCodeExecutionViewResultBlock(
                    type="text_editor_code_execution_view_result", content="line", file_type="text")),
        ],
    )


def _build() -> dict[str, Shape]:
    shapes: dict[str, Shape] = {}

    def add(shape_id: str, model: str, *, fallback: bool = False, **kwargs: Any) -> None:
        assert shape_id not in shapes, shape_id
        kwargs.setdefault("messages", [_user("Say hello.")])
        shapes[shape_id] = Shape(LLMRequest(model_name=model, **kwargs), fallback)

    # --- every model x every effort: thinking (adaptive / off floor / omitted), output_config.effort, xhigh clamp
    for model in MODELS:
        for effort in EFFORTS:
            add(f"effort/{model}/{effort or 'none'}", model, thinking=effort, system_instruction="Be brief.")

    # --- forced tool use: any (older models, no thinking), auto (5.5 / Fable reject any; thinking on)
    for model in ("claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-sonnet-5"):
        add(f"forced_tool/any/{model}", model, tools=[_tool()], force_tool_use=True)
    for model in ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"):
        add(f"forced_tool/auto/{model}", model, tools=[_tool()], force_tool_use=True)
    add("forced_tool/auto/thinking_on", "claude-sonnet-4-6", tools=[_tool()], force_tool_use=True, thinking="medium")
    add("tools/unforced_two", "claude-sonnet-5-5", tools=[_tool("lookup"), _tool("store")])
    add("tools/force_without_tools_omits_choice", "claude-sonnet-4-6", force_tool_use=True)

    # --- grounding: dynamic search tools (Sonnet/Opus) vs the legacy tools and beta header (Haiku)
    add("grounding/dynamic", "claude-sonnet-5-5", use_grounding=True)
    add("grounding/dynamic_with_tools_and_effort", "claude-opus-5-5", use_grounding=True, tools=[_tool()], thinking="high")
    add("grounding/legacy_haiku_4_5", "claude-haiku-4-5-20251001", use_grounding=True)
    add("grounding/legacy_haiku_5_5_with_tools", "claude-haiku-5-5", use_grounding=True, tools=[_tool()], thinking="low")
    add("grounding/dynamic_with_schema", "claude-sonnet-5-5", use_grounding=True, response_schema=SCHEMA)

    # --- system prompt: split at the cache boundary, whole, none, uncached
    cache = PromptCacheConfig(enabled=True)
    add("system/cache_boundary", "claude-sonnet-5-5", cache_config=cache,
        system_instruction=f"Static rules.{PROMPT_CACHE_BOUNDARY}Dynamic context.")
    add("system/cache_boundary_empty_dynamic", "claude-sonnet-5-5", cache_config=cache,
        system_instruction=f"Static rules.{PROMPT_CACHE_BOUNDARY}")
    add("system/cache_whole", "claude-sonnet-5-5", cache_config=cache, system_instruction="Static rules.")
    add("system/uncached", "claude-sonnet-5-5", system_instruction=f"One.{PROMPT_CACHE_BOUNDARY}Two.")
    add("system/none", "claude-sonnet-5-5")
    add("system/max_tokens", "claude-sonnet-5-5", max_tokens=1234)

    # --- cache_last_message: breakpoints on the last two user turns
    add("cache_last_message/two_user_turns", "claude-sonnet-5-5",
        cache_config=PromptCacheConfig(enabled=True, cache_last_message=True), system_instruction="Static rules.",
        messages=[_user("First."), _model("Reply."), _user("Second.")])
    add("cache_last_message/tool_turn", "claude-sonnet-5-5",
        cache_config=PromptCacheConfig(enabled=True, cache_last_message=True), tools=[_tool()],
        messages=_tool_turn())

    # --- history: tool call and its result, parallel results, anchors
    add("history/tool_turn", "claude-sonnet-5-5", tools=[_tool()], messages=_tool_turn())
    add("history/parallel_tool_turn", "claude-sonnet-4-6", tools=[_tool()], messages=[
        _user("Find both."),
        Message(role="model", parts=[
            MessagePart(tool_call=ToolCall(name="lookup", args={"key": "a"}, thought_signature="toolu_a")),
            MessagePart(tool_call=ToolCall(name="lookup", args={"key": "b"}, thought_signature="toolu_b")),
        ]),
        Message(role="user", parts=[
            MessagePart(tool_response={"name": "lookup", "response": "A", "tool_use_id": "toolu_a"}),
            MessagePart(tool_response={"name": "lookup", "response": "B", "tool_use_id": "toolu_b"}),
        ]),
    ])
    add("history/turn_anchor", "claude-sonnet-5-5", messages=[_user(f"{USER_TURN_SYSTEM_ANCHOR}Question one.")])
    add("history/persona_anchor", "claude-sonnet-5-5", system_instruction=PERSONA,
        messages=[_user(f"{USER_TURN_SYSTEM_ANCHOR}Question one."), _model("Answer."), _user("Question two.")])
    add("history/keyed_anchors", "claude-sonnet-5-5", system_instruction=PERSONA, turn_key="lamp-orbit-fern",
        messages=[_user(f"{USER_TURN_SYSTEM_ANCHOR}Question one.")])
    add("history/keyed_anchor_no_persona", "claude-sonnet-5-5", turn_key="lamp-orbit-fern",
        messages=[_user(f"{USER_TURN_SYSTEM_ANCHOR}Question one.")])

    # --- an assistant turn replayed from raw_content: every block type the SDK returns, passed back unchanged
    add("history/raw_content_replay", "claude-sonnet-5-5", tools=[_tool()], messages=[
        _user("Find k1."),
        Message(role="model", parts=[MessagePart(text="ignored when raw_content is set")],
                raw_content=_raw_content_blocks()),
        Message(role="user", parts=[MessagePart(text="Thanks.")]),
    ])

    # --- files: image, PDF (citations on), several parts, a file nested in a tool_result
    add("files/image", "claude-sonnet-5-5", messages=[
        Message(role="user", parts=[MessagePart(text="What is this?"), MessagePart(file_data=_file("image/png"))])])
    add("files/pdf_citations", "claude-opus-5-5", messages=[
        Message(role="user", parts=[MessagePart(file_data=_file("application/pdf")), MessagePart(text="Summarise.")])])
    add("files/unsupported_mime_skipped", "claude-sonnet-5-5", messages=[
        Message(role="user", parts=[MessagePart(text="Text."), MessagePart(file_data=_file("text/plain"))])])
    add("files/nested_in_tool_result", "claude-sonnet-5-5", tools=[_tool()], messages=[
        _user("Open it."),
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(
            name="lookup", args={"key": "k"}, thought_signature="toolu_f"))]),
        Message(role="user", parts=[
            MessagePart(tool_response={"name": "lookup", "response": "see file", "tool_use_id": "toolu_f"}),
            MessagePart(file_data=_file("image/jpeg")),
        ]),
    ])
    add("files/pdf_nested_in_tool_result", "claude-sonnet-5-5", tools=[_tool()], messages=[
        _user("Open it."),
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(
            name="lookup", args={"key": "k"}, thought_signature="toolu_g"))]),
        Message(role="user", parts=[
            MessagePart(tool_response={"name": "lookup", "response": "", "tool_use_id": "toolu_g"}),
            MessagePart(file_data=_file("application/pdf")),
        ]),
    ])

    # --- structured output: native output_config.format; with client tools the deliver_response tool instead
    add("schema/native", "claude-sonnet-5-5", response_schema=SCHEMA, system_instruction="Answer as JSON.")
    add("schema/native_with_effort", "claude-opus-5-5", response_schema=SCHEMA, thinking="medium")
    add("schema/native_older_model", "claude-sonnet-4-6", response_schema=SCHEMA)
    add("schema/native_drops_pdf_citations", "claude-sonnet-5-5", response_schema=SCHEMA, messages=[
        Message(role="user", parts=[MessagePart(file_data=_file("application/pdf")), MessagePart(text="Extract.")])])
    add("schema/native_drops_nested_pdf_citations", "claude-haiku-5-5", response_schema=SCHEMA, use_grounding=True,
        tools=[_tool()], messages=[
            _user("Open it."),
            Message(role="model", parts=[MessagePart(tool_call=ToolCall(
                name="lookup", args={"key": "k"}, thought_signature="toolu_h"))]),
            Message(role="user", parts=[
                MessagePart(tool_response={"name": "lookup", "response": "see file", "tool_use_id": "toolu_h"}),
                MessagePart(file_data=_file("application/pdf")),
            ]),
        ])
    add("schema/deliver_tool", "claude-sonnet-5-5", response_schema=SCHEMA, tools=[_tool()])
    add("schema/deliver_tool_with_effort", "claude-opus-5-5", response_schema=SCHEMA, tools=[_tool()], thinking="high")
    add("schema/deliver_tool_already_named", "claude-sonnet-5-5", response_schema=SCHEMA,
        tools=[_tool("deliver_response")])
    add("schema/non_dict_schema_ignored", "claude-sonnet-5-5", response_schema="not a schema")

    # --- the same-provider model fallback: a 529 on the first request, then the request for the next model down
    add("fallback/opus_5_5_to_sonnet_5_5", "claude-opus-5-5", fallback=True, thinking="high")
    add("fallback/sonnet_5_5_to_sonnet_5_no_thinking", "claude-sonnet-5-5", fallback=True)
    add("fallback/sonnet_5_5_to_sonnet_5_effort", "claude-sonnet-5-5", fallback=True, thinking="xhigh")
    add("fallback/sonnet_5_to_sonnet_4_6_clamp", "claude-sonnet-5", fallback=True, thinking="xhigh")
    add("fallback/with_native_schema", "claude-opus-5-5", fallback=True, response_schema=SCHEMA, thinking="low")
    add("fallback/with_native_schema_no_effort", "claude-sonnet-5", fallback=True, response_schema=SCHEMA)
    add("fallback/with_deliver_tool", "claude-sonnet-5-5", fallback=True, response_schema=SCHEMA, tools=[_tool()])
    add("fallback/forced_tool", "claude-sonnet-5", fallback=True, tools=[_tool()], force_tool_use=True)
    return shapes


SHAPES: dict[str, Shape] = _build()
