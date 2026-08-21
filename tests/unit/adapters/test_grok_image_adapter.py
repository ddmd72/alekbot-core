"""
Unit tests for GrokImageAdapter.

Pattern: mock adapter's AsyncOpenAI client at the SDK boundary (images.generate /
images.edit), capture kwargs, assert on them. Mirrors tests/unit/adapters/test_grok_adapter.py
but for the /v1/images/* surface, not /v1/responses.
"""
import base64
import io
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.grok_image_adapter import GrokImageAdapter
from src.ports.image_generation_port import GeneratedImage

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
    assert captured["extra_body"] == {"aspect_ratio": "16:9"}
    assert result == [GeneratedImage(data=_FAKE_PNG_BYTES, mime_type="image/png")]


@pytest.mark.asyncio
async def test_generate_default_aspect_ratio_is_auto(adapter):
    captured = {}

    async def mock_generate(**kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.images.generate = AsyncMock(side_effect=mock_generate)

    await adapter.generate("a red bicycle")

    assert captured["extra_body"] == {"aspect_ratio": "auto"}


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
async def test_edit_sends_correct_kwargs(adapter):
    captured = {}

    async def mock_edit(**kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.images.edit = AsyncMock(side_effect=mock_edit)

    result = await adapter.edit("remove the background", reference_images=[b"source-bytes"])

    assert captured["model"] == "grok-imagine-image-2.0"
    assert captured["prompt"] == "remove the background"
    assert captured["response_format"] == "b64_json"
    # Verify image tuple contains filename, BytesIO, and mime_type
    assert "image" in captured
    image_tuple = captured["image"]
    assert isinstance(image_tuple, tuple) and len(image_tuple) == 3
    filename, file_obj, content_type = image_tuple
    assert filename == "reference_image.png"
    assert isinstance(file_obj, type(io.BytesIO()))
    assert content_type == "image/png"
    assert result == GeneratedImage(data=_FAKE_PNG_BYTES, mime_type="image/png")


@pytest.mark.asyncio
async def test_edit_raises_on_sdk_error(adapter):
    adapter._client.images.edit = AsyncMock(side_effect=RuntimeError("network blip"))

    with pytest.raises(RuntimeError):
        await adapter.edit("remove the background", reference_images=[b"source-bytes"])


@pytest.mark.asyncio
async def test_edit_maps_jpeg_mime_type_to_jpg_extension(adapter):
    """Verify that image/jpeg mime_type is correctly mapped to .jpg filename."""
    captured = {}

    async def mock_edit(**kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.images.edit = AsyncMock(side_effect=mock_edit)

    await adapter.edit("remove the background", reference_images=[b"source-bytes"], mime_type="image/jpeg")

    image_tuple = captured["image"]
    filename, file_obj, content_type = image_tuple
    assert filename == "reference_image.jpg"
    assert content_type == "image/jpeg"


@pytest.mark.asyncio
async def test_edit_preserves_custom_mime_type(adapter):
    """Verify that custom mime_type is passed through to SDK."""
    captured = {}

    async def mock_edit(**kwargs):
        captured.update(kwargs)
        return _images_response()

    adapter._client.images.edit = AsyncMock(side_effect=mock_edit)

    await adapter.edit("remove the background", reference_images=[b"source-bytes"], mime_type="image/webp")

    image_tuple = captured["image"]
    filename, file_obj, content_type = image_tuple
    assert filename == "reference_image.webp"
    assert content_type == "image/webp"


def test_client_disables_sdk_level_retries(adapter):
    """
    Fix 3 regression guard: the SDK client must be constructed with max_retries=0.
    ImageGenerationAgent.RETRY_POLICY is NO_RETRY_POLICY specifically to avoid
    double-billing xAI on retry — an SDK-level retry (default 2) would undermine
    that policy one layer below it, invisibly.
    """
    assert adapter._client.max_retries == 0
