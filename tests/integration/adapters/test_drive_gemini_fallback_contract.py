"""Integration tests for the drive agents' Gemini fallback request shape (USER_DRIVE_INDEXING_RFC §24.2 D7 rev 3.17):
a PDF window and a triage batch go inline as bytes, and each agent's schema is enforced as a JSON schema."""
import base64

from src.adapters.gemini_adapter import GeminiAdapter
from src.agents import drive_document_reader_agent, drive_image_triage_agent
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import DICT_SCHEMA_IS_SENT_AS_JSON_SCHEMA, INLINE_FILE_BYTES_ARE_SENT
from tests.integration.adapters.conftest import GeminiCapturingStub


def _file(data: bytes, mime: str) -> MessagePart:
    return MessagePart(file_data={"base64": base64.b64encode(data).decode(), "mime_type": mime})


async def test_gemini_reader_request_contract():
    adapter = GeminiAdapter(api_key="test-key")
    stub = GeminiCapturingStub().install(adapter)
    schema = drive_document_reader_agent._RESPONSE_SCHEMA
    await adapter.generate_content(request=LLMRequest(
        model_name="gemini-3.8-flash", system_instruction="read", response_schema=schema,
        response_mime_type="application/json",
        messages=[Message(role="user", parts=[_file(b"%PDF-1.4", "application/pdf"), MessagePart(text="Read.")])]))
    INLINE_FILE_BYTES_ARE_SENT.validate("gemini", {**stub.captured_kwargs,
                                                   "expected": [(b"%PDF-1.4", "application/pdf")]})
    DICT_SCHEMA_IS_SENT_AS_JSON_SCHEMA.validate("gemini", {**stub.captured_kwargs, "expected_schema": schema})


async def test_gemini_triage_request_contract():
    adapter = GeminiAdapter(api_key="test-key")
    stub = GeminiCapturingStub().install(adapter)
    schema = drive_image_triage_agent._RESPONSE_SCHEMA
    await adapter.generate_content(request=LLMRequest(
        model_name="gemini-3.8-flash", system_instruction="triage", response_schema=schema,
        response_mime_type="application/json",
        messages=[Message(role="user", parts=[MessagePart(text="Image 1"), _file(b"\xff\xd8\xff", "image/jpeg"),
                                              MessagePart(text="Image 2"), _file(b"\x89PNG", "image/png"),
                                              MessagePart(text="Classify.")])]))
    INLINE_FILE_BYTES_ARE_SENT.validate("gemini", {**stub.captured_kwargs, "expected": [
        (b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG", "image/png")]})
    DICT_SCHEMA_IS_SENT_AS_JSON_SCHEMA.validate("gemini", {**stub.captured_kwargs, "expected_schema": schema})
