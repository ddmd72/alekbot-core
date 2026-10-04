"""Smart sends delegate_to_specialist + use_skill (+ Grok's synthesized deliver_response).
Every adapter must carry several function tools in one request."""
from unittest.mock import MagicMock

from google.genai import types as gemini_types

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.grok_adapter import GrokAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.ports.llm_port import LLMRequest, Message, MessagePart

_TOOLS = [
    {"name": "delegate_to_specialist", "description": "d",
     "parameters": {"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"]}},
    {"name": "use_skill", "description": "u",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "third_tool", "description": "t", "parameters": {"type": "object", "properties": {}}},
]
_NAMES = ["delegate_to_specialist", "use_skill", "third_tool"]


def test_claude_converts_all_tools():
    out = ClaudeAdapter(api_key="k")._convert_tools(_TOOLS)
    assert [t["name"] for t in out] == _NAMES
    assert out[1]["input_schema"]["required"] == ["name"]


def test_openai_converts_all_tools():
    out = OpenAIAdapter(api_key="k")._convert_tools(_TOOLS)
    assert [t["name"] for t in out] == _NAMES
    assert all(t["type"] == "function" for t in out)


def test_grok_converts_all_tools():
    out = GrokAdapter(api_key="k")._convert_tools(_TOOLS)
    assert [t["name"] for t in out] == _NAMES


def test_gemini_merges_functions_into_one_tool():
    out = GeminiAdapter(api_key="k")._convert_tools_to_sdk_format(_TOOLS)
    assert len(out) == 1
    assert [f.name for f in out[0].function_declarations] == _NAMES


def test_gemini_keeps_prebuilt_tools_separate():
    search = gemini_types.Tool(google_search=gemini_types.GoogleSearch())
    out = GeminiAdapter(api_key="k")._convert_tools_to_sdk_format([search] + _TOOLS)
    assert out[0] is search
    assert len(out) == 2
    assert [f.name for f in out[1].function_declarations] == _NAMES


async def test_gemini_request_carries_all_three_functions():
    adapter = GeminiAdapter(api_key="k")
    captured = {}

    async def fake_generate(model=None, contents=None, config=None):
        captured["config"] = config
        return _make_gemini_response()

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = fake_generate
    await adapter.generate_content(LLMRequest(
        model_name="gemini-flash-latest",
        messages=[Message(role="user", parts=[MessagePart(text="hi")])],
        tools=_TOOLS,
    ))

    fn_names = [f.name for t in captured["config"].tools for f in (t.function_declarations or [])]
    assert fn_names == _NAMES


def _make_gemini_response(text="OK", function_calls=None):
    """Minimal Gemini response that _parse_response can consume."""
    text_part = MagicMock()
    text_part.text = text
    text_part.function_call = None

    parts = [text_part]
    if function_calls:
        for name, args in function_calls:
            p = MagicMock()
            p.text = None
            fc = MagicMock()
            fc.name = name
            fc.args = args
            # Prevent MagicMock from auto-creating truthy thought_signature attributes.
            # _extract_thought_signature checks these via getattr and MagicMock returns
            # truthy Mock objects for any attribute, causing ToolCall pydantic validation to fail.
            fc.thought_signature = None
            fc.thoughtSignature = None
            fc.model_dump = MagicMock(return_value={})
            p.function_call = fc
            parts.append(p)

    content = MagicMock()
    content.parts = parts

    candidate = MagicMock()
    candidate.content = content
    candidate.grounding_metadata = None

    usage = MagicMock()
    usage.prompt_token_count = 10
    usage.candidates_token_count = 5
    usage.total_token_count = 15

    response = MagicMock()
    response.candidates = [candidate]
    response.usage_metadata = usage
    return response
