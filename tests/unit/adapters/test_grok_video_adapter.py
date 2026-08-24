"""
Unit tests for GrokVideoAdapter.

Pattern: mock the adapter's AsyncOpenAI client at the SDK boundary (.post()/.get()),
capture kwargs, assert on them — mirrors tests/unit/adapters/test_grok_image_adapter.py.
NEVER uses the SDK's typed .videos.* methods (see this plan's Global Constraints and
VIDEO_GENERATION_RFC.md §3.5): every call goes through the low-level escape hatch.
"""
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.grok_video_adapter import GrokVideoAdapter
from src.ports.video_generation_port import VideoPollResult
from src.ports.task_queue import TaskQueue


@pytest.fixture
def mock_task_queue():
    # Use AsyncMock() without spec=TaskQueue since enqueue_video_generation_polling
    # is added in Task 2 (doesn't exist on TaskQueue yet). Once Task 2 lands, tighten to spec=TaskQueue.
    return AsyncMock()


@pytest.fixture
def adapter(mock_task_queue):
    return GrokVideoAdapter(api_key="fake-xai-key", task_queue=mock_task_queue)


# ============================================================================
# create_video
# ============================================================================

@pytest.mark.asyncio
async def test_create_video_sends_correct_json_body(adapter):
    captured = {}

    async def mock_post(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return {"request_id": "req-abc123"}

    adapter._client.post = AsyncMock(side_effect=mock_post)

    result = await adapter.create_video(
        "a cat chasing a laser pointer", "user1", "acc1",
        duration=5, resolution="480p", aspect_ratio="16:9",
    )

    assert captured["path"] == "/videos/generations"
    body = captured["body"]
    assert body["model"] == "grok-imagine-video-1.5"
    assert body["prompt"] == "a cat chasing a laser pointer"
    assert body["duration"] == 5
    assert body["resolution"] == "480p"
    assert body["aspect_ratio"] == "16:9"
    assert "image" not in body
    assert result == "req-abc123"


@pytest.mark.asyncio
async def test_create_video_with_image_data_sends_data_uri(adapter):
    adapter._client.post = AsyncMock(return_value={"request_id": "req-xyz"})

    await adapter.create_video(
        "animate this photo", "user1", "acc1",
        image_data=b"source-bytes", image_mime_type="image/jpeg",
        duration=5, resolution="480p", aspect_ratio="16:9",
    )

    body = adapter._client.post.call_args.kwargs["body"]
    expected_b64 = base64.b64encode(b"source-bytes").decode("ascii")
    assert body["image"] == {"url": f"data:image/jpeg;base64,{expected_b64}"}


@pytest.mark.asyncio
async def test_create_video_enqueues_first_poll_tick(adapter, mock_task_queue):
    adapter._client.post = AsyncMock(return_value={"request_id": "req-abc123"})

    await adapter.create_video(
        "a sunset over mountains", "user1", "acc1",
        duration=5, resolution="480p", aspect_ratio="16:9", session_id="user1:C123",
    )

    mock_task_queue.enqueue_video_generation_polling.assert_awaited_once_with(
        request_id="req-abc123", user_id="user1", account_id="acc1",
        session_id="user1:C123", duration_s=5,
    )


@pytest.mark.asyncio
async def test_create_video_raises_on_sdk_error(adapter):
    adapter._client.post = AsyncMock(side_effect=RuntimeError("xAI 500"))

    with pytest.raises(RuntimeError):
        await adapter.create_video("a dog", "user1", "acc1")


@pytest.mark.asyncio
async def test_create_video_uses_default_duration_when_none(adapter, mock_task_queue):
    """Tests the duration is None fallback branch for billing accuracy."""
    adapter._client.post = AsyncMock(return_value={"request_id": "req-abc123"})

    await adapter.create_video(
        "a sunset over mountains", "user1", "acc1",
        resolution="480p", aspect_ratio="16:9", session_id="user1:C123",
        # Note: duration NOT passed, so duration=None
    )

    # Assert that enqueue_video_generation_polling receives the default duration_s=5
    mock_task_queue.enqueue_video_generation_polling.assert_awaited_once_with(
        request_id="req-abc123", user_id="user1", account_id="acc1",
        session_id="user1:C123", duration_s=5,
    )


# ============================================================================
# edit_video
# ============================================================================

@pytest.mark.asyncio
async def test_edit_video_sends_correct_json_body(adapter):
    adapter._client.post = AsyncMock(return_value={"request_id": "req-edit1"})

    result = await adapter.edit_video(
        "change the sky to sunset", b"video-bytes", "user1", "acc1",
        video_mime_type="video/mp4",
    )

    body = adapter._client.post.call_args.kwargs["body"]
    assert adapter._client.post.call_args.kwargs["path"] == "/videos/edits" \
        if "path" in adapter._client.post.call_args.kwargs else adapter._client.post.call_args.args[0] == "/videos/edits"
    assert body["model"] == "grok-imagine-video-1.5"
    assert body["prompt"] == "change the sky to sunset"
    expected_b64 = base64.b64encode(b"video-bytes").decode("ascii")
    assert body["video"] == {"url": f"data:video/mp4;base64,{expected_b64}"}
    assert result == "req-edit1"


@pytest.mark.asyncio
async def test_edit_video_enqueues_first_poll_tick(adapter, mock_task_queue):
    adapter._client.post = AsyncMock(return_value={"request_id": "req-edit1"})

    await adapter.edit_video("change the sky", b"video-bytes", "user1", "acc1")

    mock_task_queue.enqueue_video_generation_polling.assert_awaited_once()


# ============================================================================
# get_status
# ============================================================================

@pytest.mark.asyncio
async def test_get_status_pending(adapter):
    adapter._client.get = AsyncMock(return_value={"status": "pending"})

    result = await adapter.get_status("req-abc")

    assert result == VideoPollResult(status="pending")


@pytest.mark.asyncio
async def test_get_status_downloads_bytes_on_done(adapter):
    adapter._client.get = AsyncMock(return_value={
        "status": "done",
        "video": {"url": "https://vidgen.x.ai/output/video.mp4", "duration": 5},
    })

    mock_response = MagicMock()
    mock_response.content = b"fake-video-bytes"
    mock_response.raise_for_status = MagicMock()

    mock_http_client = AsyncMock()
    mock_http_client.get = AsyncMock(return_value=mock_response)
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.adapters.grok_video_adapter.httpx.AsyncClient", return_value=mock_http_client):
        result = await adapter.get_status("req-abc")

    assert result == VideoPollResult(status="done", data=b"fake-video-bytes")


@pytest.mark.asyncio
async def test_get_status_failed_extracts_error_message(adapter):
    adapter._client.get = AsyncMock(return_value={
        "status": "failed",
        "error": {"code": "invalid_argument", "message": "prompt violates content policy"},
    })

    result = await adapter.get_status("req-abc")

    assert result.status == "failed"
    assert result.error == "prompt violates content policy"


@pytest.mark.asyncio
async def test_get_status_expired_passes_through(adapter):
    adapter._client.get = AsyncMock(return_value={"status": "expired"})

    result = await adapter.get_status("req-abc")

    assert result == VideoPollResult(status="expired")
