"""Each adapter's native-format declaration (EXTERNAL_FILES_RFC §4.3, P5) and what it does with a file part it
declared, mocked at the SDK boundary (docs/how_to/ADAPTER_WIRE_TESTING.md): the bytes of a declared image or PDF reach
the SDK call, and Grok never sends a PDF as a file."""
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.grok_adapter import GrokAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.document_limits import native_ceiling
from src.domain.drive_image_mime import MAX_VISION_IMAGE_BYTES, VISION_MIME_TYPES
from src.domain.native_format import from_plain, to_plain
from src.ports.llm_port import LLMRequest, Message, MessagePart

_IMAGES = set(VISION_MIME_TYPES)
_PDF = b"%PDF-1.4 window"
_PNG = b"\x89PNG\r\n\x1a\n a png"


def _by_mime(formats):
    return {f.mime: f for f in formats}


def _check_images(formats, max_bytes):
    by = _by_mime(formats)
    for mime in _IMAGES:
        assert (by[mime].max_bytes, by[mime].max_pages) == (max_bytes, 1)


def test_claude_declares_base64_safe_images_and_a_pdf():
    adapter = ClaudeAdapter(api_key="k")
    for model in ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"):
        formats = adapter.native_formats(model)
        _check_images(formats, MAX_VISION_IMAGE_BYTES)
        pdf = _by_mime(formats)["application/pdf"]
        assert (pdf.max_bytes, pdf.max_pages) == (native_ceiling("pdf"), 600)
        assert set(_by_mime(formats)) == _IMAGES | {"application/pdf"}


def test_claude_haiku_takes_the_smaller_pdf_page_cap():
    formats = ClaudeAdapter(api_key="k").native_formats("claude-haiku-5-5")
    _check_images(formats, MAX_VISION_IMAGE_BYTES)
    assert _by_mime(formats)["application/pdf"].max_pages == 100


def test_openai_and_gemini_declare_full_size_images_and_a_pdf():
    for formats, pages in ((OpenAIAdapter(api_key="k").native_formats("gpt-6-luna"), 100),
                           (GeminiAdapter(api_key="k").native_formats("gemini-3.8-flash"), 1000)):
        _check_images(formats, native_ceiling("image"))
        pdf = _by_mime(formats)["application/pdf"]
        assert (pdf.max_bytes, pdf.max_pages) == (native_ceiling("pdf"), pages)


def test_grok_declares_images_only_on_its_vision_models():
    adapter = GrokAdapter(api_key="k")
    for model in ("grok-4.3", "grok-4.6", "grok-4.7"):
        formats = adapter.native_formats(model)
        _check_images(formats, native_ceiling("image"))
        assert set(_by_mime(formats)) == _IMAGES
    assert adapter.native_formats("grok-code-fast") == ()


def test_unknown_model_names_declare_nothing():
    assert ClaudeAdapter(api_key="k").native_formats("gpt-6-luna") == ()
    assert OpenAIAdapter(api_key="k").native_formats("claude-sonnet-5-5") == ()
    assert GeminiAdapter(api_key="k").native_formats("gpt-6-luna") == ()


def test_every_declaration_round_trips_json_plain():
    for formats in (ClaudeAdapter(api_key="k").native_formats("claude-haiku-5-5"),
                    OpenAIAdapter(api_key="k").native_formats("gpt-6-luna"),
                    GeminiAdapter(api_key="k").native_formats("gemini-3.8-flash"),
                    GrokAdapter(api_key="k").native_formats("grok-4.3")):
        assert from_plain(to_plain(formats)) == formats


def _request(model, mime, data):
    return LLMRequest(model_name=model, system_instruction="read", messages=[Message(role="user", parts=[
        MessagePart(file_data={"base64": base64.b64encode(data).decode(), "mime_type": mime, "name": "scan"}),
        MessagePart(text="Read it.")])])


async def test_claude_sends_a_declared_image_as_an_image_block():
    adapter = ClaudeAdapter(api_key="k")
    captured = {}
    final = MagicMock(content=[MagicMock(type="text", text="ok")], stop_reason="end_turn",
                      usage=MagicMock(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
                                      cache_read_input_tokens=0))
    stream = AsyncMock()
    stream.get_final_message = AsyncMock(return_value=final)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream)
    cm.__aexit__ = AsyncMock(return_value=None)

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream
    await adapter.generate_content(_request("claude-sonnet-5-5", "image/png", _PNG))
    block = captured["messages"][0]["content"][0]
    assert block["type"] == "image"
    assert base64.b64decode(block["source"]["data"]) == _PNG


async def test_openai_sends_a_declared_pdf_as_an_input_file():
    adapter = OpenAIAdapter(api_key="k")
    resp = MagicMock(status="completed", output=[], output_text="ok")
    resp.usage = MagicMock(input_tokens=1, output_tokens=1, total_tokens=2,
                           input_tokens_details=MagicMock(cached_tokens=0, cache_write_tokens=0),
                           output_tokens_details=MagicMock(reasoning_tokens=0))
    adapter.client.responses.create = AsyncMock(return_value=resp)
    await adapter.generate_content(_request("gpt-6-luna", "application/pdf", _PDF))
    part = adapter.client.responses.create.call_args.kwargs["input"][-1]["content"][0]
    assert part["type"] == "input_file"
    assert base64.b64decode(part["file_data"].split(",", 1)[1]) == _PDF


async def test_gemini_sends_a_declared_pdf_as_inline_bytes():
    adapter = GeminiAdapter(api_key="k")
    captured = {}
    gpart = MagicMock(text="ok", thought=False, function_call=None)
    candidate = MagicMock(grounding_metadata=None, finish_reason=None)
    candidate.content.parts = [gpart]
    response = MagicMock(candidates=[candidate])
    response.usage_metadata = MagicMock(prompt_token_count=1, candidates_token_count=1, total_token_count=2,
                                        cached_content_token_count=0, thoughts_token_count=0)

    async def generate(model=None, contents=None, config=None):
        captured["contents"] = contents
        return response

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = generate
    await adapter.generate_content(_request("gemini-3.8-flash", "application/pdf", _PDF))
    inline = captured["contents"][0].parts[0].inline_data
    assert inline.data == _PDF and inline.mime_type == "application/pdf"


async def test_grok_sends_a_declared_image_but_never_a_pdf_as_a_file():
    for mime, data in (("image/png", _PNG), ("application/pdf", _PDF)):
        adapter = GrokAdapter(api_key="k")
        captured = {}
        resp = MagicMock(output_text="ok", output=[])
        resp.usage = MagicMock(input_tokens=1, output_tokens=1, total_tokens=2)

        async def create(**kwargs):
            captured.update(kwargs)
            return resp

        adapter.client = MagicMock()
        adapter.client.responses.create = create
        await adapter.generate_content(_request("grok-4.6", mime, data))
        content = captured["input"][0]["content"]
        kinds = [c["type"] for c in content] if isinstance(content, list) else ["input_text"]
        if mime == "image/png":
            img = next(c for c in content if c["type"] == "input_image")
            assert base64.b64decode(img["image_url"].split(",", 1)[1]) == _PNG
        else:
            assert "input_file" not in kinds and "input_image" not in kinds
            assert not any(base64.b64encode(_PDF).decode() in str(c) for c in (content if isinstance(content, list) else [content]))
