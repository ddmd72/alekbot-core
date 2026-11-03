"""deliver_video(): a failed notification must not stop billing."""
from unittest.mock import AsyncMock

import pytest

from src.services.video_generation_delivery import deliver_video


@pytest.mark.asyncio
async def test_notify_failure_still_records_usage():
    storage = AsyncMock()
    notification = AsyncMock()
    notification.notify_document_link.side_effect = RuntimeError("channel down")
    quota = AsyncMock()

    await deliver_video(
        video_data=b"mp4", user_id="user1", account_id="acc1", duration_s=5,
        media_storage=storage, notification=notification, quota_service=quota,
    )

    quota.record_usage.assert_awaited_once()
    assert quota.record_usage.call_args.kwargs["account_id"] == "acc1"


@pytest.mark.asyncio
async def test_record_usage_failure_is_logged_not_raised(caplog):
    import logging

    quota = AsyncMock()
    quota.record_usage.side_effect = RuntimeError("ledger-down")

    with caplog.at_level(logging.ERROR):
        await deliver_video(
            video_data=b"mp4", user_id="user1", account_id="acc1", duration_s=5,
            media_storage=AsyncMock(), notification=AsyncMock(), quota_service=quota,
        )

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("record_usage failed" in m and "ledger-down" in m for m in errors)
