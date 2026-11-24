"""Where a task is created from (decision record external_task_confirmation_and_alerts)."""
from src.domain.external_task import ExternalTask
from src.domain.external_task_entry import TaskEntry
from src.domain.external_task_views import TaskSummary


def test_the_entry_is_chat_or_reminder_and_nothing_else():
    assert {e.value for e in TaskEntry} == {"chat", "reminder"}
    assert TaskEntry("chat") is TaskEntry.CHAT and TaskEntry.REMINDER == "reminder"


def test_neither_the_task_nor_its_summary_carries_a_taint_field():
    assert "created_tainted" not in ExternalTask.model_fields
    assert "created_tainted" not in TaskSummary.__dataclass_fields__
