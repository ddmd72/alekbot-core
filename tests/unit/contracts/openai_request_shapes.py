"""The request shapes OpenAIAdapter.generate_content builds, one `LLMRequest` each (P17 request conformance).

`SHAPES` is the table `test_openai_request_conformance.py` runs through the real adapter and the real SDK with the HTTP
transport replaced. A shape is an `LLMRequest` the production code can send; the adapter decides what reaches the wire.
Coverage rules (checked by the meta-tests): every kept field and union variant of the OpenAI contract is used by at
least one shape. Every example is generic and technical.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.llm import PROMPT_CACHE_BOUNDARY, USER_TURN_SYSTEM_ANCHOR
from src.ports.llm_port import LLMRequest, Message, MessagePart, PromptCacheConfig, ToolCall

PATH = "/responses"
_EFFORTS = (None, "none", "low", "medium", "high", "xhigh")
# One non-GPT-6 reasoning model per clamp/retention rule, one non-reasoning model.
_EXTRA_MODELS = ("gpt-5.5-pro", "gpt-5.4", "gpt-4.1")
_PNG = "iVBORw0KGgo="
_PDF = "JVBERi0xLjQK"


@dataclass(frozen=True)
class Shape:
    id: str
    request: LLMRequest
    path: str = PATH


def _user(text: str) -> Message:
    return Message(role="user", parts=[MessagePart(text=text)])


def _req(model: str, **kw) -> LLMRequest:
    kw.setdefault("messages", [_user("Summarise the build log.")])
    return LLMRequest(model_name=model, **kw)


_TOOLS = [
    {"name": "lookup_value", "description": "Look a value up by key.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "ping"},
]
_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "items": {"type": "array",
           "items": {"type": "string"}}}, "required": ["title"]}


def _history() -> list[Message]:
    return [
        _user("Look up the key alpha."),
        Message(role="model", parts=[MessagePart(text="Looking it up."),
                MessagePart(tool_call=ToolCall(name="lookup_value", args={"key": "alpha"}, thought_signature="call_a1"))]),
        Message(role="user", parts=[MessagePart(tool_response={"name": "lookup_value", "response": {"value": 7}})]),
        Message(role="model", parts=[MessagePart(text="The value is 7.")]),
        _user("Thanks. Now the key beta."),
    ]


def _raw_output_items() -> list:
    """The output items of a previous response as the SDK parsed them, passed back as they came (raw_content)."""
    from openai.types.responses import (
        ResponseFunctionToolCall, ResponseFunctionWebSearch, ResponseOutputMessage, ResponseOutputText,
        ResponseReasoningItem,
    )
    from openai.types.responses.response_reasoning_item import Content, Summary
    from openai.types.responses.response_function_web_search import ActionSearch
    return [
        ResponseReasoningItem(type="reasoning", id="rs_1", summary=[Summary(type="summary_text", text="Checked the key.")],
                              content=[Content(type="reasoning_text", text="The key exists.")]),
        ResponseFunctionWebSearch(type="web_search_call", id="ws_1", status="completed",
                                  action=ActionSearch(type="search", queries=["build log format"])),
        ResponseOutputMessage(type="message", id="msg_1", role="assistant", status="completed",
                              content=[ResponseOutputText(type="output_text", text="Here is the summary.",
                                                          annotations=[], logprobs=[])]),
        ResponseFunctionToolCall(type="function_call", id="fc_1", call_id="call_b2", name="lookup_value",
                                 arguments="{\"key\": \"beta\"}", status="completed"),
    ]


def _build() -> list[Shape]:
    shapes: list[Shape] = []

    def add(sid: str, request: LLMRequest) -> None:
        shapes.append(Shape(sid, request))

    models = list(dict.fromkeys([*OpenAIAdapter.MODEL_TIERS.values(), *_EXTRA_MODELS]))
    for model in models:
        for effort in _EFFORTS:
            add(f"effort/{model}/{effort}", _req(model, thinking=effort, max_tokens=2048))

    luna, sol = "gpt-6-luna", "gpt-6.1-sol"
    old = "gpt-5.4"

    # tools
    add("tools/auto", _req(sol, tools=_TOOLS))
    add("tools/forced", _req(sol, tools=_TOOLS, force_tool_use=True))
    # grounding: web_search tool, forced low effort, no text.format even with a schema
    add("grounding/plain", _req(old, use_grounding=True))
    add("grounding/with-tools-and-schema", _req(sol, use_grounding=True, tools=_TOOLS, response_schema=_SCHEMA,
                                                  response_mime_type="application/json"))
    add("grounding/pro-clamp", _req("gpt-5.5-pro", use_grounding=True))
    # structured output
    add("format/json-schema", _req(sol, response_schema=_SCHEMA, response_mime_type="application/json"))
    add("format/json-schema+tools", _req(luna, response_schema=_SCHEMA, tools=_TOOLS))
    add("format/json-object", _req(luna, response_mime_type="application/json"))
    # caching and storage
    add("cache/gpt-6-key", _req(sol, cache_config=PromptCacheConfig(enabled=True),
                                system_instruction=f"Static head.{PROMPT_CACHE_BOUNDARY}Dynamic tail."))
    add("cache/gpt-6-key-unmarked", _req(luna, cache_config=PromptCacheConfig(enabled=True),
                                         system_instruction="Static head only."))
    add("cache/retention-24h", _req(old, cache_config=PromptCacheConfig(enabled=True), system_instruction="Head."))
    add("cache/disabled", _req(sol, cache_config=PromptCacheConfig(enabled=False), system_instruction="Head."))
    add("store/false", _req(sol, allow_provider_storage=False))
    add("limits/timeout-and-max-tokens", _req(sol, timeout=120, max_tokens=4096, system_instruction="Be brief."))
    # history
    add("history/turns-and-function-calls", _req(sol, messages=_history(), tools=_TOOLS))
    add("history/raw-output-items", _req(sol, tools=_TOOLS, messages=[
        _user("Look up the key beta."),
        Message(role="model", parts=[MessagePart(text="ignored when raw_content is set")], raw_content=_raw_output_items()),
        Message(role="user", parts=[MessagePart(tool_response={"name": "lookup_value", "response": "9"})]),
    ]))
    add("history/image-part", _req(sol, messages=[Message(role="user", parts=[
        MessagePart(text="What is in this image?"),
        MessagePart(file_data={"mime_type": "image/png", "base64": _PNG})])]))
    add("history/pdf-part", _req(sol, messages=[Message(role="user", parts=[
        MessagePart(text="Summarise this file."),
        MessagePart(file_data={"mime_type": "application/pdf", "base64": _PDF, "name": "report.pdf"})])]))
    add("history/pdf-part-unnamed", _req(sol, messages=[Message(role="user", parts=[
        MessagePart(file_data={"mime_type": "application/pdf", "base64": _PDF})])]))
    add("history/model-turn-with-image", _req(sol, messages=[
        _user("Hello."),
        Message(role="model", parts=[MessagePart(text="Here is a picture."),
                                     MessagePart(file_data={"mime_type": "image/png", "base64": _PNG})]),
        _user("Describe it.")]))
    # anchors: the turn anchor moves to a developer item; JSON mode adds its own
    add("anchors/turn-anchor", _req(sol, system_instruction="## Persona\nA terse engineer.\n", messages=[
        _user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nWhat is the status?")]))
    add("anchors/turn-anchor+json-object", _req(luna, response_mime_type="application/json", messages=[
        _user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nReturn the status.")]))
    return shapes


SHAPES: list[Shape] = _build()
