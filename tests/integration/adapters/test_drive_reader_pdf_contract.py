"""Integration tests for the drive reader's request shape (USER_DRIVE_INDEXING_RFC §24.2): a PDF window with a
response_schema goes inline on both allowed providers, and Claude sends it without citations."""
import base64

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import PDF_FILE_INPUT_IS_INLINE, STRUCTURED_OUTPUT_PDF_SENDS_NO_CITATIONS
from tests.integration.adapters.conftest import ClaudeCapturingStub, OpenAIResponsesCapturingStub

_SCHEMA = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}


def _request(model: str) -> LLMRequest:
    pdf = {"base64": base64.b64encode(b"%PDF-1.4").decode(), "mime_type": "application/pdf",
           "name": "window-001.pdf"}
    return LLMRequest(model_name=model, system_instruction="read", response_schema=_SCHEMA,
                      response_mime_type="application/json",
                      messages=[Message(role="user", parts=[MessagePart(file_data=pdf),
                                                            MessagePart(text="Transcribe.")])])


async def test_claude_reader_request_contract():
    adapter = ClaudeAdapter(api_key="test-key")
    stub = ClaudeCapturingStub().install(adapter)
    await adapter.generate_content(request=_request("claude-haiku-5-5"))
    PDF_FILE_INPUT_IS_INLINE.validate("claude", stub.captured_kwargs)
    STRUCTURED_OUTPUT_PDF_SENDS_NO_CITATIONS.validate("claude", stub.captured_kwargs)


async def test_openai_reader_request_contract():
    adapter = OpenAIAdapter(api_key="test-key")
    stub = OpenAIResponsesCapturingStub().install(adapter)
    await adapter.generate_content(request=_request("gpt-6-luna"))
    PDF_FILE_INPUT_IS_INLINE.validate("openai", stub.captured_kwargs)
