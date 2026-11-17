"""The platform workers of WorkerHandler: the Slack worker arrives at construction, the Telegram worker through
`register_telegram_worker` (the late Telegram binding); without one, its delivery is a 503."""
from unittest.mock import AsyncMock, MagicMock

from src.handlers.worker_handler import WorkerHandler

SLACK_EVENT = {"event": {"type": "message"}, "session_id": "user-1:chan-1"}
TELEGRAM_UPDATE = {"task_type": "telegram_update", "update": {"update_id": 1}}


def _handler(slack_worker=None) -> WorkerHandler:
    return WorkerHandler(agent_worker_handler=MagicMock(), email_indexing_service=MagicMock(),
                         consolidation_service=MagicMock(), agent_factory=MagicMock(), user_repo=MagicMock(),
                         slack_worker=slack_worker)


async def test_the_slack_worker_passed_at_construction_gets_the_queued_event_with_its_headers():
    slack = AsyncMock(return_value=({"ok": True}, 202))
    h = _handler(slack_worker=slack)

    assert await h.handle(dict(SLACK_EVENT), headers={"x-trace-id": "t"}) == ({"ok": True}, 202)
    slack.assert_awaited_once_with(SLACK_EVENT, {"x-trace-id": "t"})
    assert "<queued slack event>" not in h.task_types()


async def test_the_slack_worker_never_gets_a_telegram_update():
    slack = AsyncMock()
    h = _handler(slack_worker=slack)

    assert await h.handle(dict(TELEGRAM_UPDATE)) == ({"error": "telegram not configured"}, 503)
    slack.assert_not_awaited()


async def test_a_registered_telegram_worker_gets_the_update_payload():
    h = _handler()
    telegram = AsyncMock(return_value=({"ok": True}, 200))

    assert await h.handle(dict(TELEGRAM_UPDATE)) == ({"error": "telegram not configured"}, 503)
    h.register_telegram_worker(telegram)

    assert await h.handle(dict(TELEGRAM_UPDATE)) == ({"ok": True}, 200)
    telegram.assert_awaited_once_with(TELEGRAM_UPDATE)


async def test_registering_the_telegram_worker_leaves_the_slack_worker_in_place():
    slack = AsyncMock(return_value=({"ok": True}, 200))
    telegram = AsyncMock(return_value=({"ok": True}, 200))
    h = _handler(slack_worker=slack)

    h.register_telegram_worker(telegram)

    assert await h.handle(dict(SLACK_EVENT)) == ({"ok": True}, 200)
    slack.assert_awaited_once_with(SLACK_EVENT, {})
    telegram.assert_not_awaited()
