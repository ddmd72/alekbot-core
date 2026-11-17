"""build_worker_tasks: the runner registry `WorkerHandler` receives from composition (S5b)."""
import logging

import pytest

from src.services.deep_research_failed_task import DeepResearchFailedTask
from src.services.deep_research_notice_sender import DeepResearchNoticeSender
from src.services.deep_research_webhook_event_task import DeepResearchWebhookEventTask
from src.services.disabled_task import DisabledTask
from src.services.execute_reminder_task import ExecuteReminderTask
from src.composition.worker_setup import build_worker_tasks
from src.services.deep_research_delivery import deliver_deep_research
from src.services.video_generation_delivery import deliver_video
from src.services.video_generation_polling_task import VideoGenerationPollingTask


class _Factory:
    async def ensure_agents_for_user(self, user_id: str) -> None:
        return None


def _deps(notes_port, **overrides):
    factory = _Factory()
    deps = dict(notes_port=notes_port, user_repo=object(), ensure_agents=factory.ensure_agents_for_user,
                notification_service=object(), alert_sink=object(), task_dispatch=None, video_registry=None,
                quota_service=None, media_storage=None, link_service=None, short_link_service=None,
                deep_research_fetch=None, prompt_content_store=None)
    deps.update(overrides)
    return deps


def test_with_a_note_store_the_reminder_runner_holds_exactly_the_given_dependencies():
    deps = _deps(object())

    tasks = build_worker_tasks(**deps)

    assert set(tasks) == {"execute_reminder"}
    runner = tasks["execute_reminder"]
    assert type(runner) is ExecuteReminderTask
    assert runner._notes_port is deps["notes_port"]
    assert runner._user_repo is deps["user_repo"]
    assert runner._ensure_agents == deps["ensure_agents"]
    assert runner._notification is deps["notification_service"]
    assert runner._alert_sink is deps["alert_sink"]


async def test_without_a_note_store_the_task_answers_the_refusal_it_gave_inside_the_handler(caplog):
    tasks = build_worker_tasks(**_deps(None))

    assert set(tasks) == {"execute_reminder"}
    assert type(tasks["execute_reminder"]) is DisabledTask
    payload = {"task_type": "execute_reminder", "note_id": "n1", "user_id": "user-1", "due_at": "2026-05-01T06:05:00"}
    with caplog.at_level(logging.WARNING):
        assert await tasks["execute_reminder"](payload, {}) == ({"error": "notes_port not configured"}, 501)
    assert caplog.messages == ["[Worker] execute_reminder: notes_port not configured"]


def _video_deps():
    return dict(task_dispatch=object(), video_registry=object(), quota_service=object(), media_storage=object(),
                link_service=object(), short_link_service=object())


def test_with_a_registry_and_a_dispatcher_the_video_runner_holds_exactly_the_given_dependencies():
    deps = _deps(None, **_video_deps())

    runner = build_worker_tasks(**deps)["video_generation_polling"]

    assert type(runner) is VideoGenerationPollingTask
    assert runner._video_registry is deps["video_registry"]
    assert runner._task_dispatch is deps["task_dispatch"]
    assert runner._ensure_agents == deps["ensure_agents"]
    assert runner._notification is deps["notification_service"]
    assert runner._media_storage is deps["media_storage"]
    assert runner._quota_service is deps["quota_service"]
    assert runner._link_service is deps["link_service"]
    assert runner._short_link_service is deps["short_link_service"]
    assert runner._deliver_video is deliver_video


@pytest.mark.parametrize("missing", ["video_registry", "task_dispatch"])
async def test_without_a_video_registry_or_a_dispatcher_the_task_answers_the_refusal_it_gave_inside_the_handler(
        missing, caplog):
    tasks = build_worker_tasks(**_deps(None, **{**_video_deps(), missing: None}))

    assert type(tasks["video_generation_polling"]) is DisabledTask
    with caplog.at_level(logging.WARNING):
        assert await tasks["video_generation_polling"]({"request_id": "req-1"}, {}) == (
            {"error": "video_generation not configured"}, 500)
    assert caplog.messages == ["[Worker] video_generation_polling: video_registry or task_dispatch not configured"]


def test_the_failure_notice_runner_is_always_built_and_shares_its_sender_with_the_webhook_runner():
    deps = _deps(None, deep_research_fetch=object())

    tasks = build_worker_tasks(**deps)

    failed, webhook = tasks["deep_research_failed"], tasks["deep_research_webhook_event"]
    assert type(failed) is DeepResearchFailedTask and type(webhook) is DeepResearchWebhookEventTask
    assert failed._ensure_agents == deps["ensure_agents"]
    assert type(failed._notice_sender) is DeepResearchNoticeSender
    assert failed._notice_sender is webhook._notice_sender
    assert failed._notice_sender._notification is deps["notification_service"]
    assert failed._notice_sender._alert_sink is deps["alert_sink"]


def test_with_retrieval_configured_the_webhook_runner_holds_exactly_the_given_dependencies():
    deps = _deps(None, deep_research_fetch=object(), prompt_content_store=object(), task_dispatch=object(),
                 media_storage=object(), link_service=object())

    runner = build_worker_tasks(**deps)["deep_research_webhook_event"]

    assert runner._fetch is deps["deep_research_fetch"]
    assert runner._ensure_agents == deps["ensure_agents"]
    assert runner._prompt_content_store is deps["prompt_content_store"]
    assert runner._task_dispatch is deps["task_dispatch"]
    assert runner._media_storage is deps["media_storage"]
    assert runner._link_service is deps["link_service"]
    assert runner._notification is deps["notification_service"]
    assert runner._deliver_report is deliver_deep_research


async def test_without_retrieval_the_webhook_task_answers_the_refusal_it_gave_inside_the_handler(caplog):
    tasks = build_worker_tasks(**_deps(None))

    assert type(tasks["deep_research_webhook_event"]) is DisabledTask
    with caplog.at_level(logging.WARNING):
        assert await tasks["deep_research_webhook_event"]({"event_type": "response.completed"}, {}) == (
            {"error": "retrieval not configured"}, 422)
    assert caplog.messages == ["[Worker] deep_research_webhook_event: OpenAI retrieval not configured"]
