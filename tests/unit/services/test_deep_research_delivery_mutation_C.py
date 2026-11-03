"""Mutation-kill tests (group C): round 2 and single-report notifications forward every identity kwarg."""

from unittest.mock import AsyncMock

from src.ports.media_storage_port import MediaStoragePort
from src.services.deep_research_delivery import deliver_deep_research

USER = "user-mut-1"
ACCT = "acct-mut-1"
CHAN = "chan-override-x"
PLATFORM = "slack"


async def test_round2_notification_forwards_identity_and_overrides():
    media = AsyncMock(spec=MediaStoragePort)
    notification = AsyncMock()

    await deliver_deep_research(
        result_text="verified text", round1_text="raw text",
        user_id=USER, account_id=ACCT, query="q", task_queue=None,
        media_storage=media, notification=notification,
        channel_id_override=CHAN, platform_override=PLATFORM,
    )

    calls = notification.notify_document_link.call_args_list
    round2 = next(c for c in calls if c.kwargs["label"].startswith("Round 2"))
    assert round2.kwargs["user_id"] == USER
    assert round2.kwargs["account_id"] == ACCT
    assert round2.kwargs["channel_id_override"] == CHAN
    assert round2.kwargs["platform_override"] == PLATFORM


async def test_single_report_notification_forwards_user_id():
    media = AsyncMock(spec=MediaStoragePort)
    notification = AsyncMock()

    await deliver_deep_research(
        result_text="only text", user_id=USER, account_id=ACCT, query="q",
        task_queue=None, media_storage=media, notification=notification,
        channel_id_override=CHAN, platform_override=PLATFORM,
    )

    calls = notification.notify_document_link.call_args_list
    report = next(c for c in calls if c.kwargs["label"].startswith("Research report"))
    assert report.kwargs["user_id"] == USER
    assert report.kwargs["account_id"] == ACCT
