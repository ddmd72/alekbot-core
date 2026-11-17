"""build_worker_tasks: the runner registry `WorkerHandler` receives from composition (S5b)."""
import logging

from src.infrastructure.notification_sla import dispatch_deadline_s
from src.domain.notification_kind import NotificationKind
from src.services.billing_daily_summary_task import BillingDailySummaryTask
from src.services.daily_email_review_task import DailyEmailReviewTask
from src.services.disabled_task import DisabledTask
from src.services.start_daily_email_review_task import StartDailyEmailReviewTask
from src.services.execute_reminder_task import ExecuteReminderTask
from src.composition.worker_setup import build_worker_tasks


class _Factory:
    async def ensure_agents_for_user(self, user_id: str) -> None:
        return None


def _deps(notes_port):
    factory = _Factory()
    return dict(notes_port=notes_port, user_repo=object(), ensure_agents=factory.ensure_agents_for_user,
                notification_service=object(), alert_sink=object(), email_review=object(), task_dispatch=object(),
                daily_review_marks=object(), gmail_reconnect_flag=object(), account_repo=object())


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
    assert review._alert_sink is deps["alert_sink"]


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
