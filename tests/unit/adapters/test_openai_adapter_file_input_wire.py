"""Wire tests: file parts on the OpenAI Responses API (USER_DRIVE_INDEXING_RFC §24.2). A PDF goes inline as an
`input_file` with a filename and a base64 data URL; nothing is uploaded to the Files API (an upload was never
deleted). A part that cannot be built raises instead of vanishing from the request. Mock boundary:
AsyncOpenAI.responses.create / files.create."""
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.exceptions import LLMClientError
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import PDF_FILE_INPUT_IS_INLINE

_PDF_BYTES = b"%PDF-1.4 window"
_B64 = base64.b64encode(_PDF_BYTES).decode()


def _response():
    resp = MagicMock()
    resp.status = "completed"
    resp.output = []
    resp.output_text = '{"text": "x"}'
    resp.usage = MagicMock(input_tokens=10, output_tokens=5, total_tokens=15,
                           input_tokens_details=MagicMock(cached_tokens=0, cache_write_tokens=0),
                           output_tokens_details=MagicMock(reasoning_tokens=0))
    return resp


def _adapter():
    adapter = OpenAIAdapter(api_key="k")
    adapter.client.responses.create = AsyncMock(return_value=_response())
    adapter.client.files.create = AsyncMock(side_effect=AssertionError("Files API must not be used"))
    return adapter


def _request(*parts):
    return LLMRequest(model_name="gpt-6-luna", system_instruction="read",
                      messages=[Message(role="user", parts=list(parts))])


async def test_pdf_from_base64_goes_inline_with_its_name():
    adapter = _adapter()
    await adapter.generate_content(_request(
        MessagePart(file_data={"base64": _B64, "mime_type": "application/pdf", "name": "window-001.pdf"}),
        MessagePart(text="Transcribe.")))
    kwargs = adapter.client.responses.create.call_args.kwargs
    PDF_FILE_INPUT_IS_INLINE.validate("openai", kwargs)
    part = kwargs["input"][-1]["content"][0]
    assert part == {"type": "input_file", "filename": "window-001.pdf",
                    "file_data": f"data:application/pdf;base64,{_B64}"}
    adapter.client.files.create.assert_not_called()


async def test_pdf_from_a_path_is_read_and_named_after_the_file(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(_PDF_BYTES)
    adapter = _adapter()
    await adapter.generate_content(_request(MessagePart(file_data={"path": str(path),
                                                                   "mime_type": "application/pdf"})))
    kwargs = adapter.client.responses.create.call_args.kwargs
    PDF_FILE_INPUT_IS_INLINE.validate("openai", kwargs)
    assert kwargs["input"][-1]["content"][0]["filename"] == "scan.pdf"


async def test_unnamed_base64_pdf_gets_a_neutral_filename():
    adapter = _adapter()
    await adapter.generate_content(_request(MessagePart(file_data={"base64": _B64, "mime_type": "application/pdf"})))
    assert adapter.client.responses.create.call_args.kwargs["input"][-1]["content"][0]["filename"] == "document.pdf"


async def test_unreadable_path_raises_instead_of_dropping_the_part(tmp_path):
    adapter = _adapter()
    with pytest.raises(LLMClientError):
        await adapter.generate_content(_request(MessagePart(file_data={"path": str(tmp_path / "gone.pdf"),
                                                                       "mime_type": "application/pdf"})))
    adapter.client.responses.create.assert_not_called()


async def test_unreadable_image_raises_instead_of_dropping_the_part(tmp_path):
    adapter = _adapter()
    with pytest.raises(LLMClientError):
        await adapter.generate_content(_request(MessagePart(file_data={"path": str(tmp_path / "gone.jpg"),
                                                                       "mime_type": "image/jpeg"})))


async def test_file_part_without_content_raises():
    adapter = _adapter()
    with pytest.raises(LLMClientError):
        await adapter.generate_content(_request(MessagePart(file_data={"mime_type": "application/pdf"})))


async def test_reference_only_part_is_still_skipped():
    adapter = _adapter()
    await adapter.generate_content(_request(MessagePart(text='[File: "a.docx"]'),
                                            MessagePart(file_data={"ref": "a.docx", "mime_type": "text/plain"})))
    assert adapter.client.responses.create.call_args.kwargs["input"][-1]["content"] == '[File: "a.docx"]'


async def test_image_still_goes_as_input_image():
    adapter = _adapter()
    await adapter.generate_content(_request(MessagePart(file_data={"base64": "abc", "mime_type": "image/png"})))
    part = adapter.client.responses.create.call_args.kwargs["input"][-1]["content"][0]
    assert part == {"type": "input_image", "image_url": "data:image/png;base64,abc"}
