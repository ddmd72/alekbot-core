"""The production graph's `execute_reminder` runner holds the graph's own objects (S5b; the P9 net does not see a wrong
object of the right type)."""
import pytest

from src.services.billing_daily_summary_task import BillingDailySummaryTask
from src.services.daily_email_review_task import DailyEmailReviewTask
from src.services.execute_reminder_task import ExecuteReminderTask
from src.services.gmail_reconnect_flag import GmailReconnectFlag
from src.services.start_daily_email_review_task import StartDailyEmailReviewTask
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
