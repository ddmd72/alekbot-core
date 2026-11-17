"""The production graph's `execute_reminder` runner holds the graph's own objects (S5b; the P9 net does not see a wrong
object of the right type)."""
import pytest

from src.services.billing_daily_summary_task import BillingDailySummaryTask
from src.services.daily_email_review_task import DailyEmailReviewTask
from src.services.deep_research_delivery import deliver_deep_research
from src.services.deep_research_failed_task import DeepResearchFailedTask
from src.services.deep_research_webhook_event_task import DeepResearchWebhookEventTask
from src.services.execute_reminder_task import ExecuteReminderTask
from src.services.gmail_reconnect_flag import GmailReconnectFlag
from src.services.start_daily_email_review_task import StartDailyEmailReviewTask
from tests.support import worker_triggers as wt
from src.services.video_generation_delivery import deliver_video
from src.services.video_generation_polling_task import VideoGenerationPollingTask
from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow


async def test_the_reminder_runner_is_built_from_the_graphs_note_store_users_agents_notifier_and_ops_sink(
        prod_graph: ProdGraph):
    app = prod_graph.app
    runner = app.worker_handler._tasks["execute_reminder"]

    assert type(runner) is ExecuteReminderTask
    assert runner._notes_port is app.container.notes_adapter
    assert runner._notes_port is app.worker_handler._reminders_service._notes_port
    assert runner._user_repo is app.user_repo
    assert runner._ensure_agents == app.agent_factory.ensure_agents_for_user
    assert runner._notification is app.notification_service
    assert runner._alert_sink is app.alert_sink and runner._alert_sink is not None


async def test_the_static_reading_of_the_registry_is_the_production_handlers_registry(prod_graph: ProdGraph):
    """`worker_triggers` reads the task types and runner classes from source; this ties that reading to the built graph,
    so a runner registered another way (or a class the reader names wrongly) fails here."""
    handler = prod_graph.app.worker_handler
    composed = wt.composed_runners()

    assert handler.task_types() == wt.dispatch_branches()
    for task_type, runners in composed.items():
        assert type(handler._tasks[task_type]).__name__ in {r.cls for r in runners}, task_type
    assert set(handler._tasks) - set(wt.registry_in_source(wt.WORKER_HANDLER.read_text())) == set(composed)


async def test_the_video_runner_is_built_from_the_graphs_registry_dispatcher_storage_quota_and_link_services(
        prod_graph: ProdGraph):
    app = prod_graph.app
    runner = app.worker_handler._tasks["video_generation_polling"]

    assert type(runner) is VideoGenerationPollingTask
    assert runner._video_registry is app.agent_factory.video_registry and runner._video_registry is not None
    assert runner._task_dispatch is app.task_dispatch and runner._task_dispatch is not None
    assert runner._ensure_agents == app.agent_factory.ensure_agents_for_user
    assert runner._notification is app.notification_service
    assert runner._media_storage is app.media_storage
    assert runner._quota_service is app.quota_service
    assert runner._link_service is app.agent_worker_handler._link_service
    assert runner._short_link_service is app.short_link_service
    assert runner._deliver_video is deliver_video


async def test_the_deep_research_runners_are_built_from_the_graphs_fetch_store_dispatcher_and_one_shared_sender(
        prod_graph: ProdGraph):
    app = prod_graph.app
    failed = app.worker_handler._tasks["deep_research_failed"]
    webhook = app.worker_handler._tasks["deep_research_webhook_event"]

    assert type(failed) is DeepResearchFailedTask and type(webhook) is DeepResearchWebhookEventTask
    assert failed._ensure_agents == webhook._ensure_agents == app.agent_factory.ensure_agents_for_user
    assert failed._notice_sender is webhook._notice_sender
    assert failed._notice_sender._notification is app.notification_service
    assert failed._notice_sender._alert_sink is app.alert_sink and failed._notice_sender._alert_sink is not None
    assert webhook._fetch == app.deep_research_fetch and webhook._fetch is not None
    assert webhook._prompt_content_store is app.container.prompt_content_store
    assert webhook._task_dispatch is app.task_dispatch
    assert webhook._media_storage is app.media_storage
    assert webhook._link_service is app.agent_worker_handler._link_service
    assert webhook._notification is app.notification_service
    assert webhook._deliver_report is deliver_deep_research


async def test_the_review_runners_are_built_from_the_graphs_review_service_queue_marks_users_agents_notifier_and_flag(
        prod_graph: ProdGraph):
    app = prod_graph.app
    handler = app.worker_handler
    fan_out = handler._tasks["start_daily_email_review"]
    review = handler._tasks["daily_email_review"]
    flag = handler._gmail_reconnect_flag

    assert type(fan_out) is StartDailyEmailReviewTask
    assert fan_out._email_review is app.container.email_review_service and fan_out._email_review is not None
    assert fan_out._task_dispatch is handler._task_dispatch and fan_out._task_dispatch is not None
    assert fan_out._user_repo is app.user_repo
    assert fan_out._marks is not None
    assert type(review) is DailyEmailReviewTask
    assert review._email_review is app.container.email_review_service
    assert review._ensure_agents == app.agent_factory.ensure_agents_for_user
    assert review._notification is app.notification_service
    assert review._alert_sink is app.alert_sink and review._alert_sink is not None
    assert type(flag) is GmailReconnectFlag and review._gmail_reconnect_flag is flag
    assert flag._oauth_credentials is app.container.oauth_credentials and flag._oauth_credentials is not None
    assert flag._mail_connector is app.container.connectors.get("gmail")
    assert flag._notification is app.notification_service
    assert flag._ensure_agents == app.agent_factory.ensure_agents_for_user


async def test_the_billing_runner_is_built_from_the_graphs_ops_sink_account_store_and_users(prod_graph: ProdGraph):
    app = prod_graph.app
    runner = app.worker_handler._tasks["billing_daily_summary"]

    assert type(runner) is BillingDailySummaryTask
    assert runner._billing_webhook is app.alert_sink and runner._billing_webhook is not None
    assert runner._account_repo is app.account_repo
    assert runner._user_repo is app.user_repo
