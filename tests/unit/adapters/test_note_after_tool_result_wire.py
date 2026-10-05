"""Plan delta D6: a text part after tool results in one user message must convert
to a valid request on every provider (ADAPTER_WIRE_TESTING.md — mock at the SDK boundary)."""
from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.grok_adapter import GrokAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.llm import Message, MessagePart, ToolCall
from tests.contracts.adapter_contracts import NOTE_AFTER_TOOL_RESULT_SURVIVES_CONVERSION

NOTE = "[System: meanwhile in chat]\n- user: also Tuesday"


def _history():
    return [
        Message(role="user", parts=[MessagePart(text="compare offers")]),
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(
            name="delegate_to_specialist", args={"intent": "search_web", "query": "q"},
            thought_signature="call_1"))]),
        Message(role="user", parts=[
            MessagePart(tool_response={"name": "delegate_to_specialist", "response": "r",
                                       "tool_use_id": "call_1"}),
            MessagePart(text=NOTE),
        ]),
    ]


async def test_claude_tool_result_first_then_text():
    out = await ClaudeAdapter(api_key="k")._convert_messages(_history())
    blocks = out[-1]["content"]
    assert out[-1]["role"] == "user"
    assert blocks[0]["type"] == "tool_result"
    assert blocks[-1]["type"] == "text" and "also Tuesday" in blocks[-1]["text"]
    NOTE_AFTER_TOOL_RESULT_SURVIVES_CONVERSION.validate("claude", {"messages": out})


async def test_openai_function_output_then_user_text():
    items = await OpenAIAdapter(api_key="k")._convert_input(_history())
    i_out = next(i for i, it in enumerate(items) if it.get("type") == "function_call_output")
    later = [it for it in items[i_out + 1:] if it.get("role") == "user"]
    assert later and "also Tuesday" in str(later[0]["content"])
    NOTE_AFTER_TOOL_RESULT_SURVIVES_CONVERSION.validate("openai", {"input": items})


def test_grok_function_output_then_user_text():
    items = GrokAdapter(api_key="k")._convert_input(_history())
    i_out = next(i for i, it in enumerate(items) if it.get("type") == "function_call_output")
    later = [it for it in items[i_out + 1:] if it.get("role") == "user"]
    assert later and "also Tuesday" in str(later[0]["content"])
    NOTE_AFTER_TOOL_RESULT_SURVIVES_CONVERSION.validate("grok", {"input": items})


async def test_gemini_function_response_and_text_in_one_user_turn():
    contents = await GeminiAdapter(api_key="k")._convert_messages(_history())
    last = contents[-1]
    assert last.role == "user"
    assert any(getattr(p, "function_response", None) for p in last.parts)
    assert any(getattr(p, "text", None) and "also Tuesday" in p.text for p in last.parts)
    NOTE_AFTER_TOOL_RESULT_SURVIVES_CONVERSION.validate("gemini", {"contents": contents})
