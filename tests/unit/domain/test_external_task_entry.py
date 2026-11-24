"""Where a task is created from (decision record external_task_confirmation_and_alerts)."""
import pytest

from src.domain.external_task import ExternalTask
from src.domain.external_task_entry import REMINDER_RUN_ORIGIN, TaskEntry, task_entry
from src.domain.external_task_views import TaskSummary


def test_the_entry_is_chat_or_reminder_and_nothing_else():
    assert {e.value for e in TaskEntry} == {"chat", "reminder"}
    assert TaskEntry("chat") is TaskEntry.CHAT and TaskEntry.REMINDER == "reminder"


def test_neither_the_task_nor_its_summary_carries_a_taint_field():
    assert "created_tainted" not in ExternalTask.model_fields
    assert "created_tainted" not in TaskSummary.__dataclass_fields__


@pytest.mark.parametrize("context,expected", [
    ({"interactive_delivery": True}, TaskEntry.CHAT),
    ({"run_origin": REMINDER_RUN_ORIGIN}, TaskEntry.REMINDER),
    ({"interactive_delivery": True, "run_origin": REMINDER_RUN_ORIGIN}, TaskEntry.CHAT),    # a present owner wins
    ({"run_origin": "background:daily_email_review"}, None),
    ({"run_origin": "background:external_task_deliver"}, None),
    ({"run_origin": "voice:delegate"}, None),
    ({"interactive_delivery": None, "run_origin": None}, None),
    ({}, None),
])
def test_the_entry_comes_from_the_run_context_only(context, expected):
    assert task_entry(context) is expected
