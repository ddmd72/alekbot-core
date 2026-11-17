"""build_worker_tasks: the runner registry `WorkerHandler` receives from composition (S5b)."""
import logging

import pytest

from src.infrastructure.notification_sla import dispatch_deadline_s
from src.domain.notification_kind import NotificationKind
from src.services.background_empty_answer_reporter import BackgroundEmptyAnswerReporter
from src.services.billing_daily_summary_task import BillingDailySummaryTask
from src.services.daily_email_review_task import DailyEmailReviewTask
from src.services.deep_research_failed_task import DeepResearchFailedTask
from src.services.deep_research_notice_sender import DeepResearchNoticeSender
from src.services.disabled_task import DisabledTask
from src.services.start_daily_email_review_task import StartDailyEmailReviewTask
from src.services.execute_reminder_task import ExecuteReminderTask
from src.composition.worker_setup import build_worker_tasks
from src.services.video_generation_delivery import deliver_video
from src.services.video_generation_polling_task import VideoGenerationPollingTask


# Every task type is registered whatever is configured: an absent dependency gives a refusal, never an unknown type.
_TASK_TYPES = {"execute_reminder", "video_generation_polling", "deep_research_failed", "deep_research_webhook_event",
               "start_daily_email_review", "daily_email_review", "billing_daily_summary"}


class _Factory:
    async def ensure_agents_for_user(self, user_id: str) -> None:
        return None


def _deps(notes_port, **overrides):
    factory = _Factory()
    deps = dict(notes_port=notes_port, user_repo=object(), ensure_agents=factory.ensure_agents_for_user,
                notification_service=object(), alert_sink=object(), task_dispatch=object(), video_registry=None,
                quota_service=None, media_storage=None, link_service=None, short_link_service=None,
                deep_research_fetch=None, prompt_content_store=None, email_review=object(),
                daily_review_marks=object(), gmail_reconnect_flag=object(), account_repo=object())
    deps.update(overrides)
    return deps


def test_with_a_note_store_the_reminder_runner_holds_exactly_the_given_dependencies():
    deps = _deps(object())

    tasks = build_worker_tasks(**deps)

    assert set(tasks) == _TASK_TYPES
    runner = tasks["execute_reminder"]
    assert type(runner) is ExecuteReminderTask
    assert runner._notes_port is deps["notes_port"]
    assert runner._user_repo is deps["user_repo"]
    assert runner._ensure_agents == deps["ensure_agents"]
    assert runner._notification is deps["notification_service"]
    assert runner._empty_answer_reporter._alert_sink is deps["alert_sink"]


async def test_without_a_note_store_the_task_answers_the_refusal_it_gave_inside_the_handler(caplog):
    tasks = build_worker_tasks(**_deps(None))

    assert set(tasks) == _TASK_TYPES
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

    failed = tasks["deep_research_failed"]
    assert type(failed) is DeepResearchFailedTask
    assert failed._ensure_agents == deps["ensure_agents"]
    assert type(failed._notice_sender) is DeepResearchNoticeSender
    assert failed._notice_sender._notification is deps["notification_service"]
    assert failed._notice_sender._empty_answer_reporter._alert_sink is deps["alert_sink"]


def test_with_the_review_dependencies_the_fan_out_and_the_review_runners_hold_exactly_the_given_ones():
    deps = _deps(object())

    tasks = build_worker_tasks(**deps)

    fan_out = tasks["start_daily_email_review"]
    assert type(fan_out) is StartDailyEmailReviewTask
    assert fan_out._email_review is deps["email_review"]
    assert fan_out._task_dispatch is deps["task_dispatch"]
    assert fan_out._user_repo is deps["user_repo"]
    assert fan_out._marks is deps["daily_review_marks"]
    assert fan_out._deadline_seconds == dispatch_deadline_s(NotificationKind.DAILY_DIGEST)
    review = tasks["daily_email_review"]
    assert type(review) is DailyEmailReviewTask
    assert review._email_review is deps["email_review"]
    assert review._ensure_agents == deps["ensure_agents"]
    assert review._notification is deps["notification_service"]
    assert review._gmail_reconnect_flag is deps["gmail_reconnect_flag"]
    assert review._empty_answer_reporter._alert_sink is deps["alert_sink"]



def test_the_reminder_review_and_notice_runners_share_one_empty_answer_reporter_over_the_given_sink():
    deps = _deps(object(), deep_research_fetch=object())

    tasks = build_worker_tasks(**deps)

    reporter = tasks["execute_reminder"]._empty_answer_reporter
    assert type(reporter) is BackgroundEmptyAnswerReporter
    assert reporter._alert_sink is deps["alert_sink"]
    assert tasks["daily_email_review"]._empty_answer_reporter is reporter
    assert tasks["deep_research_failed"]._notice_sender._empty_answer_reporter is reporter

async def test_without_the_review_service_both_review_tasks_answer_the_refusals_they_gave_inside_the_handler(caplog):
    tasks = build_worker_tasks(**{**_deps(object()), "email_review": None})

    assert type(tasks["start_daily_email_review"]) is DisabledTask
    assert type(tasks["daily_email_review"]) is DisabledTask
    with caplog.at_level(logging.WARNING):
        assert await tasks["start_daily_email_review"]({"task_type": "start_daily_email_review"}, {}) == (
            {"error": "services not configured"}, 501)
        assert await tasks["daily_email_review"]({"user_id": "user-1", "account_id": "acct-1"}, {}) == (
            {"error": "email_review_service not configured"}, 501)
    assert caplog.messages == ["[Worker] start_daily_email_review: required services not configured",
                               "[Worker] daily_email_review: email_review_service not configured"]


async def test_without_the_task_queue_the_fan_out_answers_its_refusal_and_the_review_still_runs(caplog):
    tasks = build_worker_tasks(**{**_deps(object()), "task_dispatch": None})

    assert type(tasks["start_daily_email_review"]) is DisabledTask
    assert type(tasks["daily_email_review"]) is DailyEmailReviewTask
    with caplog.at_level(logging.WARNING):
        assert await tasks["start_daily_email_review"]({}, {}) == ({"error": "services not configured"}, 501)
    assert caplog.messages == ["[Worker] start_daily_email_review: required services not configured"]


def test_the_fan_out_gets_no_marks_store_when_none_is_given():
    tasks = build_worker_tasks(**{**_deps(object()), "daily_review_marks": None})

    assert tasks["start_daily_email_review"]._marks is None


def test_with_the_webhook_and_the_account_store_the_billing_runner_holds_exactly_the_given_dependencies():
    deps = _deps(object())

    runner = build_worker_tasks(**deps)["billing_daily_summary"]

    assert type(runner) is BillingDailySummaryTask
    assert runner._billing_webhook is deps["alert_sink"]
    assert runner._account_repo is deps["account_repo"]
    assert runner._user_repo is deps["user_repo"]


async def test_without_the_webhook_the_billing_task_answers_the_refusal_it_gave_inside_the_handler(caplog):
    tasks = build_worker_tasks(**{**_deps(object()), "alert_sink": None, "account_repo": None})

    assert type(tasks["billing_daily_summary"]) is DisabledTask
    with caplog.at_level(logging.WARNING):
        assert await tasks["billing_daily_summary"]({}, {}) == ({"error": "billing_webhook not configured"}, 501)
    assert caplog.messages == ["[Worker] billing_daily_summary: billing_webhook not configured"]


async def test_without_the_account_store_the_billing_task_answers_the_refusal_it_gave_inside_the_handler(caplog):
    tasks = build_worker_tasks(**{**_deps(object()), "account_repo": None})

    assert type(tasks["billing_daily_summary"]) is DisabledTask
    with caplog.at_level(logging.WARNING):
        assert await tasks["billing_daily_summary"]({}, {}) == ({"error": "account_repo not configured"}, 501)
    assert caplog.messages == ["[Worker] billing_daily_summary: account_repo not configured"]
