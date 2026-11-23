"""The alert of a firing reminder carries a flagged note's text as outside content (EXTERNAL_TASK_QUEUE_RFC §6.1): the
background run reads the label and instruction as data, and an unflagged note's alert is unchanged. Alex Doe's
reminder about Shelbyville irrigation kits.
"""
import re
from datetime import datetime, timezone

from src.domain.agent_note import AgentNote
from src.domain.reminder_alert import build_reminder_alert, build_reminder_alert_summary
from src.domain.untrusted_text import EXTERNAL_REMINDER, has_untrusted_wrapper

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)


def note(**over) -> AgentNote:
    fields = dict(note_id="1729401234567", user_id="usr-1", text="compare irrigation kits", created_at=NOW, due=NOW,
                  instruction="Compare community-garden irrigation kits for Shelbyville and tell Alex.")
    return AgentNote(**{**fields, **over})


def test_a_flagged_note_is_wrapped_in_the_alert():
    alert = build_reminder_alert(note(untrusted=True))
    nonce = re.search(r'nonce="([0-9a-f]{16})"', alert).group(1)
    tag = f'<external_content nonce="{nonce}" ref="agent_note:1729401234567" name="reminder">'
    assert alert.count(tag) == 2 and EXTERNAL_REMINDER in alert and has_untrusted_wrapper(alert)
    assert f"{tag}\nCompare community-garden irrigation kits for Shelbyville and tell Alex.\n</external_content" in alert
    assert "note_id: 1729401234567" in alert and "[SELF-REMINDER]" in alert      # code-written lines stay outside


def test_an_unflagged_note_alert_is_unchanged():
    alert = build_reminder_alert(note())
    assert "external_content" not in alert and EXTERNAL_REMINDER not in alert
    assert alert.startswith('[SELF-REMINDER] "compare irrigation kits"\n')
    assert "\nCompare community-garden irrigation kits for Shelbyville and tell Alex.\n" in alert


def test_the_history_label_of_a_flagged_note_does_not_carry_its_text():
    assert "irrigation" not in build_reminder_alert_summary(note(untrusted=True))
    assert build_reminder_alert_summary(note()) == 'Received reminder: "compare irrigation kits"'
