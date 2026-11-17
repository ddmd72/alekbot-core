"""The production graph's `execute_reminder` runner holds the graph's own objects (S5b; the P9 net does not see a wrong
object of the right type)."""
import pytest

from src.services.execute_reminder_task import ExecuteReminderTask
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
