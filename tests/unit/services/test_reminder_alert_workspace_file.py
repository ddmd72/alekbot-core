"""The fire-time alert carries exactly one new line, `Workspace file: workspace:<path>`, right after Schedule, and
nothing else changes (AGENT_WORKSPACE_RFC §8.3, D15). The signature stays build_reminder_alert(note)."""
import inspect
from datetime import datetime, timezone

from src.domain.agent_note import AgentNote
from src.services.reminders_service import build_reminder_alert

FILE = "projects/morning-briefing.md"
LINE = f"Workspace file: workspace:{FILE}"


def _note(**kw):
    return AgentNote(note_id="1729401234567", user_id="u1", text="Morning briefing",
                     instruction="Morning briefing: follow the workspace file.",
                     created_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
                     due=datetime(2026, 10, 20, 6, tzinfo=timezone.utc), **kw)


def _after_schedule(alert):
    lines = alert.splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("Schedule: "))
    return lines[i + 1:]


def test_one_line_after_the_schedule():
    alert = build_reminder_alert(_note(workspace_file=FILE))
    assert _after_schedule(alert)[:3] == [LINE, "", "Morning briefing: follow the workspace file."]


def test_the_line_and_no_other():
    plain = build_reminder_alert(_note())
    bound = build_reminder_alert(_note(workspace_file=FILE))
    assert bound.replace(LINE + "\n", "", 1) == plain
    assert "Workspace file:" not in plain


def test_the_signature_is_unchanged():
    assert list(inspect.signature(build_reminder_alert).parameters) == ["note"]
