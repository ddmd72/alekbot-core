"""Forwarding tests for deep_research_delivery: every identity kwarg reaches its collaborator."""

from unittest.mock import AsyncMock, MagicMock

from src.ports.media_storage_port import MediaStoragePort
from src.services.deep_research_delivery import deliver_deep_research

USER = "user-mut-1"
ACCT = "acct-mut-1"
CHAN = "chan-override-x"
PLATFORM = "slack"


async def test_upload_round_builds_link_for_owner_and_key():
    media = AsyncMock(spec=MediaStoragePort)
    link_service = MagicMock()
    link_service.build_link.return_value = "https://example.test/f/tok"

    await deliver_deep_research(
        result_text="report body", user_id=USER, account_id=ACCT, query="q",
        task_queue=None, media_storage=media, link_service=link_service,
    )

    link_service.build_link.assert_called_once()
    kwargs = link_service.build_link.call_args.kwargs
    assert kwargs["user_id"] == USER
    assert kwargs["key"].startswith(f"deep_research/{USER}/")


async def test_round1_notification_forwards_identity_and_overrides():
    media = AsyncMock(spec=MediaStoragePort)
    notification = AsyncMock()

    await deliver_deep_research(
        result_text="verified text", round1_text="raw text",
        user_id=USER, account_id=ACCT, query="q", task_queue=None,
        media_storage=media, notification=notification,
        channel_id_override=CHAN, platform_override=PLATFORM,
    )

    calls = notification.notify_document_link.call_args_list
    round1 = next(c for c in calls if c.kwargs["label"].startswith("Round 1"))
    assert round1.kwargs["user_id"] == USER
    assert round1.kwargs["account_id"] == ACCT
    assert round1.kwargs["channel_id_override"] == CHAN
    assert round1.kwargs["platform_override"] == PLATFORM
