"""
Unit tests for GrokImageAdapter.

Pattern: mock adapter's AsyncOpenAI client at the SDK boundary (images.generate /
images.edit), capture kwargs, assert on them. Mirrors tests/unit/adapters/test_grok_adapter.py
but for the /v1/images/* surface, not /v1/responses.
"""
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.grok_image_adapter import GrokImageAdapter
from src.ports.image_generation_port import GeneratedImage, ReferenceImage

_FAKE_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00fake"
_FAKE_B64 = base64.b64encode(_FAKE_PNG_BYTES).decode("ascii")


def _image_item(b64=_FAKE_B64):
    item = MagicMock()
    item.b64_json = b64
    return item


def _images_response(items=None):
    response = MagicMock()
    response.data = items if items is not None else [_image_item()]
    return response


@pytest.fixture
def adapter():
    return GrokImageAdapter(api_key="fake-xai-key")


@pytest.mark.asyncio
async def test_generate_sends_correct_kwargs(adapter):
    captured = {}

    async def mock_generate(**kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.images.generate = AsyncMock(side_effect=mock_generate)

    result = await adapter.generate("a red bicycle", aspect_ratio="16:9", n=1)

    assert captured["model"] == "grok-imagine-image-2.0"
    assert captured["prompt"] == "a red bicycle"
    assert captured["n"] == 1
    assert captured["response_format"] == "b64_json"
    assert captured["extra_body"] == {
        "aspect_ratio": "16:9",
        "resolution": "1k",
        "quality": "medium",
    }
    assert result == [GeneratedImage(data=_FAKE_PNG_BYTES, mime_type="image/png")]


@pytest.mark.asyncio
async def test_generate_default_aspect_ratio_is_auto(adapter):
    captured = {}

    async def mock_generate(**kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.images.generate = AsyncMock(side_effect=mock_generate)

    await adapter.generate("a red bicycle")

    assert captured["extra_body"] == {
        "aspect_ratio": "auto",
        "resolution": "1k",
        "quality": "medium",
    }


@pytest.mark.asyncio
async def test_generate_sends_explicit_resolution_and_quality(adapter):
    captured = {}

    async def mock_generate(**kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.images.generate = AsyncMock(side_effect=mock_generate)

    await adapter.generate("a red bicycle", resolution="2k", quality="low")

    assert captured["extra_body"]["resolution"] == "2k"
    assert captured["extra_body"]["quality"] == "low"


@pytest.mark.asyncio
async def test_generate_default_resolution_and_quality(adapter):
    captured = {}

    async def mock_generate(**kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.images.generate = AsyncMock(side_effect=mock_generate)

    await adapter.generate("a red bicycle")

    assert captured["extra_body"]["resolution"] == "1k"
    assert captured["extra_body"]["quality"] == "medium"


@pytest.mark.asyncio
async def test_generate_decodes_multiple_images(adapter):
    second_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00second"
    second_b64 = base64.b64encode(second_bytes).decode("ascii")
    adapter._client.images.generate = AsyncMock(
        return_value=_images_response([_image_item(), _image_item(second_b64)])
    )

    result = await adapter.generate("two bicycles", n=2)

    assert len(result) == 2
    assert result[1].data == second_bytes


@pytest.mark.asyncio
async def test_generate_returns_empty_list_on_sdk_error(adapter):
    adapter._client.images.generate = AsyncMock(side_effect=RuntimeError("network blip"))

    result = await adapter.generate("a red bicycle")

    assert result == []


@pytest.mark.asyncio
async def test_edit_sends_correct_json_body(adapter):
    """
    edit() must NOT use the SDK's images.edit() convenience method — that method
    always sends multipart/form-data, which xAI's /v1/images/edits rejects with
    HTTP 415 (confirmed live in production 2026-08-21). It must go through the
    low-level AsyncOpenAI.post() escape hatch instead, which sends a plain JSON body.
    """
    captured = {}

    async def mock_post(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return _images_response()

    adapter._client.post = AsyncMock(side_effect=mock_post)

    ref = ReferenceImage(data=b"source-bytes", mime_type="image/png")
    result = await adapter.edit("remove the background", reference_images=[ref])

    assert captured["path"] == "/images/edits"
    body = captured["body"]
    assert body["model"] == "grok-imagine-image-2.0"
    assert body["prompt"] == "remove the background"
    assert body["response_format"] == "b64_json"
    # xAI's JSON shape for a reference image: {"url": <data-uri>, "type": "image_url"}
    assert body["image"]["type"] == "image_url"
    expected_b64 = base64.b64encode(b"source-bytes").decode("ascii")
    assert body["image"]["url"] == f"data:image/png;base64,{expected_b64}"
    assert "images" not in body
    # Confirmed live 2026-08-24 (see class docstring in domain/billing.py) that
    # /v1/images/edits accepts resolution/quality — sent with their defaults.
    assert body["resolution"] == "1k"
    assert body["quality"] == "medium"
    assert result == GeneratedImage(data=_FAKE_PNG_BYTES, mime_type="image/png")


@pytest.mark.asyncio
async def test_edit_sends_explicit_resolution_and_quality(adapter):
    captured = {}

    async def mock_post(path, **kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.post = AsyncMock(side_effect=mock_post)
    ref = ReferenceImage(data=b"source-bytes", mime_type="image/png")

    await adapter.edit("remove the background", reference_images=[ref], resolution="2k", quality="low")

    assert captured["body"]["resolution"] == "2k"
    assert captured["body"]["quality"] == "low"


@pytest.mark.asyncio
async def test_edit_raises_on_sdk_error(adapter):
    adapter._client.post = AsyncMock(side_effect=RuntimeError("network blip"))
    ref = ReferenceImage(data=b"source-bytes", mime_type="image/png")

    with pytest.raises(RuntimeError):
        await adapter.edit("remove the background", reference_images=[ref])


@pytest.mark.asyncio
async def test_edit_uses_jpeg_mime_type_in_data_uri(adapter):
    """Verify that a reference's own mime_type is correctly embedded in its data URI."""
    captured = {}

    async def mock_post(path, **kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.post = AsyncMock(side_effect=mock_post)
    ref = ReferenceImage(data=b"source-bytes", mime_type="image/jpeg")

    await adapter.edit("remove the background", reference_images=[ref])

    assert captured["body"]["image"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_edit_preserves_custom_mime_type(adapter):
    """Verify a non-standard mime_type is passed through into the data URI unchanged."""
    captured = {}

    async def mock_post(path, **kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.post = AsyncMock(side_effect=mock_post)
    ref = ReferenceImage(data=b"source-bytes", mime_type="image/webp")

    await adapter.edit("remove the background", reference_images=[ref])

    assert captured["body"]["image"]["url"].startswith("data:image/webp;base64,")


@pytest.mark.asyncio
async def test_edit_multi_reference_uses_images_array_field(adapter):
    """2-3 references use a DIFFERENT, plural "images" array field — mutually
    exclusive with the singular "image" field used for exactly 1 reference."""
    captured = {}

    async def mock_post(path, **kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.post = AsyncMock(side_effect=mock_post)
    refs = [
        ReferenceImage(data=b"bytes-a", mime_type="image/jpeg"),
        ReferenceImage(data=b"bytes-b", mime_type="image/png"),
        ReferenceImage(data=b"bytes-c", mime_type="image/jpeg"),
    ]

    await adapter.edit("combine these", reference_images=refs)

    body = captured["body"]
    assert "image" not in body
    assert len(body["images"]) == 3
    assert body["images"][0]["url"] == (
        f"data:image/jpeg;base64,{base64.b64encode(b'bytes-a').decode('ascii')}"
    )
    assert body["images"][1]["url"] == (
        f"data:image/png;base64,{base64.b64encode(b'bytes-b').decode('ascii')}"
    )
    assert all(img["type"] == "image_url" for img in body["images"])


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 4])
async def test_edit_rejects_out_of_range_reference_count(adapter, count):
    refs = [ReferenceImage(data=b"x", mime_type="image/png") for _ in range(count)]

    with pytest.raises(ValueError, match="1-3"):
        await adapter.edit("edit this", reference_images=refs)


def test_client_disables_sdk_level_retries(adapter):
    """
    Fix 3 regression guard: the SDK client must be constructed with max_retries=0.
    ImageGenerationAgent.RETRY_POLICY is NO_RETRY_POLICY specifically to avoid
    double-billing xAI on retry — an SDK-level retry (default 2) would undermine
    that policy one layer below it, invisibly.
    """
    assert adapter._client.max_retries == 0
