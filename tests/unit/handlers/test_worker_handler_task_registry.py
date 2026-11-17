"""WorkerHandler dispatches through its task registry: task type -> WorkerTask (composition-root refactor S5a).

The table below is the dispatch chain the registry replaced: each task type and the method that serves it, with
whether the method takes the payload.
"""
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.worker_task import WorkerTask
from src.handlers.worker_handler import WorkerHandler

TASKS = {
    "agent_execution": ("_handle_agent_execution", True),
    "telegram_update": ("_handle_telegram_update", True),
    "email_indexing": ("_handle_email_indexing", True),
    "email_indexing_watchdog": ("_handle_watchdog", False),
    "consolidation": ("_handle_consolidation", True),
    "sweep_consolidation": ("_handle_sweep_consolidation", False),
    "companion_consolidation": ("_handle_companion_consolidation", True),
    "sweep_companion_consolidation": ("_handle_sweep_companion_consolidation", False),
    "deep_research_failed": ("_handle_deep_research_failed", True),
    "deep_research_webhook_event": ("_handle_deep_research_webhook_event", True),
    "video_generation_polling": ("_handle_video_generation_polling", True),
    "setup_microsoft_todo": ("_handle_setup_microsoft_todo", True),
    "reindex_task_list": ("_handle_reindex_task_list", True),
    "renew_task_subscriptions": ("_handle_renew_task_subscriptions", True),
    "renew_all_task_subscriptions": ("_handle_renew_all_task_subscriptions", False),
    "fire_due_reminders": ("_handle_fire_due_reminders", False),
    "start_email_indexing": ("_handle_start_email_indexing", False),
    "start_daily_email_review": ("_handle_start_daily_email_review", False),
    "daily_email_review": ("_handle_daily_email_review", True),
    "billing_daily_summary": ("_handle_billing_daily_summary", False),
    "start_drive_sync": ("_handle_start_drive_sync", False),
    "reconnect_notice_sweep": ("_handle_reconnect_notice_sweep", False),
    "fact_lifecycle_sweep": ("_handle_fact_lifecycle_sweep", False),
    "fact_review": ("_handle_fact_review", True),
    "interactive_choice": ("_handle_interactive_choice", True),
}
SLACK_EVENT = {"event": {"type": "message"}, "session_id": "user-1:chan-1"}
UNKNOWN = {"error": "unknown task_type"}


def _handler(agent_worker=None, agent_factory=None) -> WorkerHandler:
    return WorkerHandler(
        agent_worker_handler=agent_worker or MagicMock(),
        email_indexing_service=MagicMock(),
        notification_service=MagicMock(),
        consolidation_service=MagicMock(),
        agent_factory=agent_factory or MagicMock(),
        user_repo=MagicMock(),
    )


def test_the_registry_holds_every_task_type():
    assert _handler().task_types() == frozenset(TASKS)


@pytest.mark.parametrize("task_type", sorted(TASKS))
async def test_each_task_type_runs_its_method(task_type, monkeypatch):
    name, takes_payload = TASKS[task_type]
    calls = []

    async def method(self, *args):
        calls.append((self, args))
        return {"ran": name}, 299

    monkeypatch.setattr(WorkerHandler, name, method)
    h = _handler()
    payload = {"task_type": task_type, "key": "value"}

    assert await h.handle(payload, headers={"x-trace-id": "t"}) == ({"ran": name}, 299)
    assert calls == [(h, (payload,) if takes_payload else ())]
    # The registry entry exposes the method it calls, so a reader can name each task's consumer.
    assert inspect.unwrap(h._tasks[task_type]) == getattr(h, name)


def test_every_registry_entry_is_called_as_a_worker_task():
    protocol = list(inspect.signature(WorkerTask.__call__).parameters)[1:]
    assert protocol == ["payload", "headers"]
    for key, task in _handler()._tasks.items():
        assert inspect.iscoroutinefunction(task), key
        assert list(inspect.signature(task, follow_wrapped=False).parameters) == protocol, key


@pytest.mark.parametrize("payload", [
    {"task_type": "no_such_task"},
    {"task_type": "drive_sync"},
    {},
    {"task_type": None, "event": {}},
    {"task_type": "<queued slack event>"},
    {"task_type": "<queued slack event>", **SLACK_EVENT},
    {"task_type": 5},
    {"task_type": ["email_indexing"]},
    {"task_type": {"name": "email_indexing"}},
], ids=["unregistered", "indexer-only", "no-task-type", "slack-shape-incomplete", "slack-key-sent",
        "slack-key-sent-with-event", "int", "list", "dict"])
async def test_an_unknown_task_type_is_a_400(payload):
    assert await _handler().handle(payload) == (UNKNOWN, 400)


async def test_a_queued_slack_event_without_a_slack_worker_is_a_503():
    assert await _handler().handle(dict(SLACK_EVENT)) == ({"error": "slack not configured"}, 503)


async def test_attach_slack_worker_routes_the_queued_slack_event_with_its_headers():
    h = _handler()
    slack = AsyncMock(return_value=({"ok": True}, 202))
    h.attach_slack_worker(slack)

    assert await h.handle(dict(SLACK_EVENT), headers={"x-trace-id": "t"}) == ({"ok": True}, 202)
    slack.assert_awaited_once_with(SLACK_EVENT, {"x-trace-id": "t"})
    assert "<queued slack event>" not in h.task_types()


async def test_a_telegram_update_without_a_telegram_worker_is_a_503():
    assert await _handler().handle({"task_type": "telegram_update"}) == ({"error": "telegram not configured"}, 503)


async def test_attach_telegram_worker_routes_the_update_payload():
    h = _handler()
    telegram = AsyncMock(return_value=({"ok": True}, 200))
    h.attach_telegram_worker(telegram)
    payload = {"task_type": "telegram_update", "update": {"update_id": 1}}

    assert await h.handle(payload) == ({"ok": True}, 200)
    telegram.assert_awaited_once_with(payload)


async def test_agent_execution_builds_the_users_agents_before_the_task():
    order = []
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock(side_effect=lambda user_id: order.append(("ensure", user_id)))
    worker = MagicMock()
    worker.handle_task = AsyncMock(side_effect=lambda payload: order.append(("task", payload)) or {"done": 1})
    payload = {"task_type": "agent_execution", "context": {"user_id": "user-1"}}

    assert await _handler(worker, factory).handle(payload) == ({"done": 1}, 200)
    assert order == [("ensure", "user-1"), ("task", payload)]


async def test_agent_execution_without_a_user_runs_the_task_only():
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock()
    worker = MagicMock()
    worker.handle_task = AsyncMock(return_value={"done": 1})

    assert await _handler(worker, factory).handle({"task_type": "agent_execution"}) == ({"done": 1}, 200)
    factory.ensure_agents_for_user.assert_not_awaited()
