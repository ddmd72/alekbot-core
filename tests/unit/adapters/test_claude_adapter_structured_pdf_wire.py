"""Wire tests: a PDF next to a response_schema (the drive reader, USER_DRIVE_INDEXING_RFC §24.2). Anthropic rejects
citations together with output_config.format (HTTP 400), so the document block goes without them; a PDF without a
schema keeps citations. Mock boundary: AsyncAnthropic.messages.stream."""
from unittest.mock import AsyncMock, MagicMock

from src.adapters.claude_adapter import ClaudeAdapter
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import PDF_FILE_INPUT_IS_INLINE, STRUCTURED_OUTPUT_PDF_SENDS_NO_CITATIONS

_SCHEMA = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
_PDF = {"base64": "JVBERi0xLjQK", "mime_type": "application/pdf", "name": "window-001.pdf"}


def _sdk_response(text='{"text": "x"}'):
    block = MagicMock()
    block.type = "text"
    block.text = text
    usage = MagicMock(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    response = MagicMock(content=[block], usage=usage, stop_reason="end_turn")
    return response


def _capture(adapter):
    captured = {}
    stream = AsyncMock()
    stream.get_final_message = AsyncMock(return_value=_sdk_response())
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream)
    cm.__aexit__ = AsyncMock(return_value=None)

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream
    return captured


def _request(schema):
    return LLMRequest(model_name="claude-haiku-5-5", system_instruction="read",
                      messages=[Message(role="user", parts=[MessagePart(file_data=dict(_PDF)),
                                                            MessagePart(text="Transcribe.")])],
                      response_schema=schema, response_mime_type="application/json" if schema else None)


async def test_pdf_with_a_response_schema_sends_no_citations():
    adapter = ClaudeAdapter(api_key="k")
    captured = _capture(adapter)
    await adapter.generate_content(_request(_SCHEMA))
    STRUCTURED_OUTPUT_PDF_SENDS_NO_CITATIONS.validate("claude", captured)
    PDF_FILE_INPUT_IS_INLINE.validate("claude", captured)


async def test_pdf_without_a_schema_keeps_citations():
    adapter = ClaudeAdapter(api_key="k")
    captured = _capture(adapter)
    await adapter.generate_content(_request(None))
    doc = captured["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["citations"] == {"enabled": True}


async def test_citations_are_dropped_inside_a_tool_result_too():
    adapter = ClaudeAdapter(api_key="k")
    captured = _capture(adapter)
    from src.domain.llm import ToolCall
    messages = [
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(name="open_file", args={},
                                                                    thought_signature="tu_1"))]),
        Message(role="user", parts=[MessagePart(tool_response={"name": "open_file", "response": "ok",
                                                               "tool_use_id": "tu_1"}),
                                    MessagePart(file_data=dict(_PDF))]),
    ]
    await adapter.generate_content(LLMRequest(model_name="claude-haiku-5-5", messages=messages,
                                              response_schema=_SCHEMA))
    STRUCTURED_OUTPUT_PDF_SENDS_NO_CITATIONS.validate("claude", captured)
