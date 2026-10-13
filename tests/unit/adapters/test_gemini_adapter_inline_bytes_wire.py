"""GeminiAdapter wire tests for the drive agents' fallback (USER_DRIVE_INDEXING_RFC §24.2 D7 rev 3.17), mocked at the
SDK boundary (`client.aio.models.generate_content`, docs/how_to/ADAPTER_WIRE_TESTING.md):

- inline base64 `file_data` (the drive reader's PDF window or image, the triage's thumbnails) is sent as the file's
  bytes in an `inline_data` part. Before this, the adapter logged "Unsupported file_data format" and dropped the
  part, so the model answered about an image it never saw;
- each drive agent's real response schema goes to `response_json_schema` unchanged (Gemini's JSON Schema subset
  takes `required`, `enum`, `description` and nullable type arrays), with the JSON mime type."""
import base64
from unittest.mock import MagicMock

import pytest

from src.adapters.gemini_adapter import GeminiAdapter
from src.agents import drive_document_reader_agent, drive_image_triage_agent
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import DICT_SCHEMA_IS_SENT_AS_JSON_SCHEMA, INLINE_FILE_BYTES_ARE_SENT

JPEG = b"\xff\xd8\xff\xe0 a jpeg"
PNG = b"\x89PNG\r\n\x1a\n a png"
PDF = b"%PDF-1.4 a window"


def _sdk_response(text='{"text": "x", "brief": null}'):
    part = MagicMock(text=text, thought=False, function_call=None)
    candidate = MagicMock(grounding_metadata=None, finish_reason=None)
    candidate.content.parts = [part]
    response = MagicMock(candidates=[candidate])
    response.usage_metadata = MagicMock(prompt_token_count=10, candidates_token_count=5, total_token_count=15,
                                        cached_content_token_count=0, thoughts_token_count=0)
    return response


def _adapter():
    adapter = GeminiAdapter(api_key="test-key")
    captured = {}

    async def generate(model=None, contents=None, config=None):
        captured.update(model=model, contents=contents, config=config)
        return _sdk_response()

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = generate
    return adapter, captured


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def test_an_inline_pdf_window_is_sent_as_its_bytes():
    adapter, captured = _adapter()
    await adapter.generate_content(request=LLMRequest(
        model_name="gemini-3.8-flash", system_instruction="read",
        messages=[Message(role="user", parts=[
            MessagePart(file_data={"base64": _b64(PDF), "mime_type": "application/pdf", "name": "window-001.pdf"}),
            MessagePart(text="Transcribe.")])]))
    parts = captured["contents"][0].parts
    assert parts[0].inline_data.data == PDF and parts[0].inline_data.mime_type == "application/pdf"
    assert parts[1].text == "Transcribe."
    adapter.client.files.upload.assert_not_called()
    INLINE_FILE_BYTES_ARE_SENT.validate("gemini", {**captured, "expected": [(PDF, "application/pdf")]})


async def test_a_triage_batch_keeps_every_thumbnail_in_order_next_to_its_index():
    adapter, captured = _adapter()
    parts = []
    for index, (data, mime) in enumerate([(JPEG, "image/jpeg"), (PNG, "image/png"), (JPEG, "image/jpeg")], start=1):
        parts.append(MessagePart(text=f"Image {index}: camera metadata absent"))
        parts.append(MessagePart(file_data={"base64": _b64(data), "mime_type": mime}))
    parts.append(MessagePart(text="Classify each of the 3 images above."))
    await adapter.generate_content(request=LLMRequest(model_name="gemini-3.8-flash", system_instruction="triage",
                                                      messages=[Message(role="user", parts=parts)]))
    sent = captured["contents"][0].parts
    assert len(sent) == 7   # nothing dropped: three labels, three images, the task
    assert [p.text for p in sent[0:6:2]] == [f"Image {i}: camera metadata absent" for i in (1, 2, 3)]
    INLINE_FILE_BYTES_ARE_SENT.validate("gemini", {**captured, "expected": [
        (JPEG, "image/jpeg"), (PNG, "image/png"), (JPEG, "image/jpeg")]})


async def test_an_inline_part_logs_no_unsupported_format_warning(monkeypatch):
    from src.adapters import gemini_adapter
    log = MagicMock()
    monkeypatch.setattr(gemini_adapter, "logger", log)
    adapter, _ = _adapter()
    await adapter.generate_content(request=LLMRequest(
        model_name="gemini-3.8-flash", system_instruction="s",
        messages=[Message(role="user", parts=[MessagePart(file_data={"base64": _b64(JPEG),
                                                                      "mime_type": "image/jpeg"})])]))
    assert not any("Unsupported file_data" in str(c.args[0]) for c in log.warning.call_args_list)


@pytest.mark.parametrize("schema", [drive_document_reader_agent._RESPONSE_SCHEMA,
                                    drive_image_triage_agent._RESPONSE_SCHEMA], ids=["reader", "triage"])
async def test_the_drive_agents_schema_goes_to_response_json_schema_unchanged(schema):
    adapter, captured = _adapter()
    await adapter.generate_content(request=LLMRequest(
        model_name="gemini-3.8-flash", system_instruction="s", response_mime_type="application/json",
        response_schema=schema, thinking="low", max_tokens=4096,
        messages=[Message(role="user", parts=[MessagePart(text="go")])]))
    DICT_SCHEMA_IS_SENT_AS_JSON_SCHEMA.validate("gemini", {**captured, "expected_schema": schema})
    assert captured["model"] == "gemini-3.8-flash"
    assert captured["config"].tools is None   # JSON mime type cannot be combined with function calling on Gemini


def test_the_reader_schema_uses_type_arrays_for_null():
    """The shape the Gemini rule's description vouches for: nullable as a type array, no `nullable` keyword."""
    props = drive_document_reader_agent._RESPONSE_SCHEMA["properties"]
    assert props["brief"]["type"] == ["object", "null"]
    assert "nullable" not in str(drive_document_reader_agent._RESPONSE_SCHEMA)
