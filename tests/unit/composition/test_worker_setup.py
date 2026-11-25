"""build_worker_tasks: the runner registry `WorkerHandler` receives from composition (S5b)."""
import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock

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
from src.composition.memory_inbox_setup import MemoryInbox
from src.composition.worker_setup import build_worker_tasks
from src.domain.memory_inbox_task_keys import FINAL_TASK, SWEEP_TASK, TRIAGE_TASK
from src.domain.ui_messages import UIMessage
from src.handlers.worker_handler import WorkerHandler
from src.services.memory_inbox_final_task import MemoryInboxFinalTask
from src.services.memory_inbox_sweep_task import MemoryInboxSweepTask
from src.services.memory_inbox_triage_task import MemoryInboxTriageTask
from tests.support.memory_inbox_rhythm import (
    UTC, FakePass, Languages, Notifier, ProfileStep, Rig, final_outcome, triage_outcome,
)
from src.services.video_generation_delivery import deliver_video
from src.services.video_generation_polling_task import VideoGenerationPollingTask


# Every task type is registered whatever is configured: an absent dependency gives a refusal, never an unknown type.
_TASK_TYPES = {"execute_reminder", "video_generation_polling", "deep_research_failed",
               "start_daily_email_review", "daily_email_review", "billing_daily_summary", "geo_mark_turn",
               "external_task_timer", "external_task_deliver", "external_task_wake",
               "memory_inbox_triage", "memory_inbox_final", "memory_inbox_sweep"}


class _Factory:
    async def ensure_agents_for_user(self, user_id: str) -> None:
        return None


def _deps(notes_port, **overrides):
    factory = _Factory()
    deps = dict(notes_port=notes_port, user_repo=object(), ensure_agents=factory.ensure_agents_for_user,
                notification_service=object(), alert_sink=object(), task_dispatch=object(), video_registry=None,
                quota_service=None, media_storage=None, link_service=None, short_link_service=None, email_review=object(),
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


def test_the_failure_notice_runner_is_always_built_over_its_notice_sender():
    deps = _deps(None)

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
    deps = _deps(object())

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


_INBOX_TASK_TYPES = {TRIAGE_TASK, FINAL_TASK, SWEEP_TASK}


# --- the memory inbox's three task types (MCP_MEMORY_SUBMISSIONS_RFC §10) -------------------------------------------

class _InboxWorld(Rig):
    """The rhythm kit's store, lease, queue and users, the bundle over them with scripted passes, and the registry."""

    def __init__(self) -> None:
        super().__init__(datetime.now(UTC) - timedelta(hours=3))
        self.triage_pass = FakePass(self.db, triage_outcome(candidates=2, sources=("Desk assistant",)))
        self.final_pass = FakePass(self.db, final_outcome(wrote=1))
        self.profile_step = ProfileStep(self.db)
        self.notifier = Notifier()
        self.languages = Languages()
        self.bundle = MemoryInbox(
            store=self.repo, service=MagicMock(), triage_pass=self.triage_pass, final_pass=self.final_pass,
            lease=self.lease, owner_zones=self.owner_zones, profile_step=self.profile_step,
            languages=self.languages, task_queue=self.queue, run_timeout_s=600.0)  # type: ignore[arg-type]
        self.tasks = build_worker_tasks(**_deps(object(), user_repo=self.users, account_repo=self.accounts,
                                                notification_service=self.notifier, memory_inbox=self.bundle))
        self.handler = WorkerHandler(
            agent_worker_handler=MagicMock(), email_indexing_service=MagicMock(), consolidation_service=MagicMock(),
            agent_factory=MagicMock(), user_repo=self.users, tasks=self.tasks)

    async def dispatch(self, task_type: str, **payload):
        return await self.handler.handle({"task_type": task_type, **payload})


def test_with_the_inbox_the_three_runners_hold_the_bundles_parts():
    world = _InboxWorld()
    triage, final, sweep = (world.tasks[t] for t in (TRIAGE_TASK, FINAL_TASK, SWEEP_TASK))

    assert (type(triage), type(final), type(sweep)) == (MemoryInboxTriageTask, MemoryInboxFinalTask, MemoryInboxSweepTask)
    assert triage._store is final._store is sweep._store is world.repo
    assert triage._pass is world.triage_pass and final._pass is world.final_pass
    assert triage._lease is final._lease is world.lease
    assert triage._task_queue is final._task_queue is sweep._task_queue is world.queue
    assert triage._owner_zones is final._owner_zones is sweep._owner_zones is world.owner_zones
    assert triage._user_repo is final._user_repo is sweep._user_repo is world.users
    assert triage._account_repo is sweep._account_repo is world.accounts
    assert triage._languages is world.languages and triage._notifications is world.notifier
    assert final._profile_step is world.profile_step
    assert triage._run_timeout_s == final._run_timeout_s == 600.0


@pytest.mark.parametrize("task_type", sorted(_INBOX_TASK_TYPES))
@pytest.mark.parametrize("missing", ["memory_inbox", "account_repo"])
async def test_without_the_inbox_or_the_account_store_a_memory_inbox_task_answers_501(task_type, missing):
    tasks = build_worker_tasks(**_deps(object(), **{missing: None}))

    assert type(tasks[task_type]) is DisabledTask
    assert await tasks[task_type]({"user_id": "u1"}, {}) == ({"error": "memory inbox not configured"}, 501)


async def test_the_handler_dispatches_a_triage_task_to_the_runner_and_the_owner_gets_one_notice():
    world = _InboxWorld()
    await world.received()

    body, status = await world.dispatch(TRIAGE_TASK, user_id="u1", account_id="a1")

    assert (body, status) == ({"status": "completed"}, 200)
    assert len(world.triage_pass.requests) == 1 and world.triage_pass.requests[0].account_id == "a1"
    [(user_id, account_id, message, fields)] = world.notifier.posts
    assert (user_id, account_id, message, fields["count"]) == ("u1", "a1", UIMessage.MEMORY_INBOX_CANDIDATES_FROM, "2")


async def test_the_handler_dispatches_a_final_task_to_the_runner_and_the_profile_step_runs_after_a_write():
    world = _InboxWorld()
    await world.confirmed(world.clock.now)              # confirmed three hours ago: its final run is due

    body, status = await world.dispatch(FINAL_TASK, user_id="u1", account_id="a1")

    assert (body, status) == ({"status": "completed"}, 200)
    assert len(world.final_pass.requests) == 1 and [c[0] for c in world.profile_step.calls] == ["a1"]


async def test_the_handler_dispatches_a_sweep_task_to_the_runner_and_it_enqueues_the_triage():
    world = _InboxWorld()
    await world.received()

    body, status = await world.dispatch(SWEEP_TASK)

    assert (body, status) == ({"expired": 0, "triage_enqueued": 1, "final_backstop": 0}, 200)
    assert [call["task_type"] for call in world.queue.created] == [TRIAGE_TASK]
