"""Unit tests for deliver_video()."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.video_generation_delivery import deliver_video


@pytest.fixture
def mock_media_storage():
    m = AsyncMock()
    m.store = AsyncMock(return_value=None)
    return m


@pytest.fixture
def mock_notification():
    return AsyncMock()


@pytest.fixture
def mock_link_service():
    m = MagicMock()
    m.build_link.return_value = "https://bot.example/f/token123"
    return m


@pytest.mark.asyncio
async def test_uploads_to_gcs_with_video_content_type(mock_media_storage, mock_notification):
    await deliver_video(
        video_data=b"fake-mp4-bytes", user_id="user1", account_id="acc1",
        duration_s=5, media_storage=mock_media_storage, notification=mock_notification,
    )

    mock_media_storage.store.assert_awaited_once()
    call_kwargs = mock_media_storage.store.call_args.kwargs
    assert call_kwargs["data"] == b"fake-mp4-bytes"
    assert call_kwargs["content_type"] == "video/mp4"
    assert call_kwargs["key"].startswith("video_generation/user1/")


@pytest.mark.asyncio
async def test_sends_document_link_notification(mock_media_storage, mock_notification, mock_link_service):
    await deliver_video(
        video_data=b"fake-mp4-bytes", user_id="user1", account_id="acc1",
        duration_s=5, media_storage=mock_media_storage, notification=mock_notification,
        link_service=mock_link_service,
    )

    mock_notification.notify_document_link.assert_awaited_once()
    call_kwargs = mock_notification.notify_document_link.call_args.kwargs
    assert call_kwargs["user_id"] == "user1"
    assert call_kwargs["account_id"] == "acc1"
    assert call_kwargs["url"] == "https://bot.example/f/token123"
    assert call_kwargs["label"] == "Your generated video"


@pytest.mark.asyncio
async def test_records_cost_via_quota_service(mock_media_storage, mock_notification):
    mock_quota = AsyncMock()

    await deliver_video(
        video_data=b"fake-mp4-bytes", user_id="user1", account_id="acc1",
        duration_s=5, media_storage=mock_media_storage, notification=mock_notification,
        quota_service=mock_quota,
    )

    mock_quota.record_usage.assert_awaited_once_with(
        account_id="acc1", model="grok-imagine-video-1.5", tokens=0, cost=0.40,
    )


@pytest.mark.asyncio
async def test_skips_billing_when_quota_service_unset(mock_media_storage, mock_notification):
    # Must not raise when quota_service=None (the default).
    await deliver_video(
        video_data=b"fake-mp4-bytes", user_id="user1", account_id="acc1",
        duration_s=5, media_storage=mock_media_storage, notification=mock_notification,
    )


@pytest.mark.asyncio
async def test_gcs_upload_failure_aborts_before_notification(mock_media_storage, mock_notification):
    mock_media_storage.store.side_effect = RuntimeError("GCS unavailable")

    await deliver_video(
        video_data=b"fake-mp4-bytes", user_id="user1", account_id="acc1",
        duration_s=5, media_storage=mock_media_storage, notification=mock_notification,
    )

    mock_notification.notify_document_link.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_link_failure_falls_back_to_key(mock_media_storage, mock_notification, mock_link_service):
    # link_service.build_link() raises; should fall back to key and complete delivery.
    mock_link_service.build_link.side_effect = RuntimeError("Token service unavailable")

    await deliver_video(
        video_data=b"fake-mp4-bytes", user_id="user1", account_id="acc1",
        duration_s=5, media_storage=mock_media_storage, notification=mock_notification,
        link_service=mock_link_service,
    )

    # Notification should still be sent with key as fallback URL
    mock_notification.notify_document_link.assert_awaited_once()
    call_kwargs = mock_notification.notify_document_link.call_args.kwargs
    assert call_kwargs["url"].startswith("video_generation/user1/")  # Falls back to key
    assert call_kwargs["user_id"] == "user1"
    assert call_kwargs["account_id"] == "acc1"
    assert call_kwargs["label"] == "Your generated video"
