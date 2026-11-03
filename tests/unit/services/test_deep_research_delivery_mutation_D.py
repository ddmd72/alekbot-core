"""Mutation-kill tests (group D) for deep_research_delivery: single-round forwarding and failed-notice result."""

from unittest.mock import AsyncMock

from src.ports.media_storage_port import MediaStoragePort
from src.services.deep_research_delivery import deliver_deep_research

USER = "user-mut-1"
ACCT = "acct-mut-1"
CHAN = "chan-override-x"
PLATFORM = "slack"


async def test_single_round_report_notification_forwards_identity_and_overrides():
    media = AsyncMock(spec=MediaStoragePort)
    notification = AsyncMock()
    link_service = AsyncMock()
    link_service.build_link = lambda **kw: "https://example.test/f/tok"

    await deliver_deep_research(
        result_text="only text", user_id=USER, account_id=ACCT, query="q",
        task_queue=None, media_storage=media, notification=notification,
        link_service=link_service,
        channel_id_override=CHAN, platform_override=PLATFORM,
    )

    notification.notify_document_link.assert_awaited_once()
    kwargs = notification.notify_document_link.call_args.kwargs
    assert kwargs["label"] == "Research report (raw)"
    assert kwargs["user_id"] == USER
    assert kwargs["account_id"] == ACCT
    assert kwargs["channel_id_override"] == CHAN
    assert kwargs["platform_override"] == PLATFORM


async def test_failure_notice_that_raises_still_returns_false():
    queue = AsyncMock()
    queue.enqueue_agent_task.side_effect = RuntimeError("queue down")
    notification = AsyncMock()
    notification.notify.side_effect = RuntimeError("notify down")

    result = await deliver_deep_research(
        result_text="text", user_id=USER, account_id=ACCT, query="q",
        task_queue=queue, notification=notification,
    )

    assert result is False
