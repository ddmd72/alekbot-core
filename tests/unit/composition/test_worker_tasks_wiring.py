"""The production graph's `execute_reminder` runner holds the graph's own objects (S5b; the P9 net does not see a wrong
object of the right type)."""
import pytest

from src.services.execute_reminder_task import ExecuteReminderTask
from tests.support import worker_triggers as wt
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
