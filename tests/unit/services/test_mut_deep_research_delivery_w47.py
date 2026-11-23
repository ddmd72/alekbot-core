"""The 'report not prepared' notice names the owner's account and the origin channel (mutation survivors)."""
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.deep_research_delivery import deliver_deep_research


class _RecordingNotification:
    def __init__(self):
        self.calls = []

    async def notify(self, **kwargs):
        self.calls.append(kwargs)
        return MagicMock(delivered=True, sent=True, error=None)


async def test_enqueue_failure_notice_forwards_identity_and_overrides():
    queue = AsyncMock()
    queue.enqueue_agent_task.side_effect = TimeoutError("t")
    notification = _RecordingNotification()
    with patch("src.services.deep_research_delivery.asyncio.sleep", new=AsyncMock()):
        ok = await deliver_deep_research(
            result_text="t", user_id="usr-1", account_id="acc-1", query="q",
            task_queue=queue, notification=notification, report_dedup_id="job-1",
            channel_id_override="chan-1", platform_override="telegram",
        )
    assert ok is False
    assert len(notification.calls) == 1
    call = notification.calls[0]
    assert call["user_id"] == "usr-1"
    assert call["account_id"] == "acc-1"
    assert call["channel_id_override"] == "chan-1"
    assert call["platform_override"] == "telegram"
