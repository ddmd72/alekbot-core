"""The Router hands Smart every field a delegation can change, now incl. workspace_file (AGENT_WORKSPACE_RFC §8.3);
the workspace's delete/rename warnings read workspace_file from these dicts (§8.4)."""
from datetime import datetime, timezone

from src.agents.core.router_agent import RouterAgent
from src.domain.agent_note import AgentNote

UTC = timezone.utc


def test_note_context_carries_the_workspace_file():
    note = AgentNote(note_id="1729401234567", user_id="u1", text="Morning briefing", instruction="x",
                     created_at=datetime(2026, 10, 19, tzinfo=UTC), due=datetime(2026, 10, 21, 6, tzinfo=UTC),
                     recurrence="FREQ=DAILY", workspace_file="projects/morning-briefing.md")
    assert RouterAgent._note_context(note) == {
        "note_id": "1729401234567",
        "text": "Morning briefing",
        "due": "2026-10-21T06:00:00+00:00",
        "recurrence": "FREQ=DAILY",
        "complexity": None,
        "last_fired": None,
        "workspace_file": "projects/morning-briefing.md",
    }


def test_an_unset_field_is_none():
    note = AgentNote(note_id="1", user_id="u1", text="x", instruction="x",
                     created_at=datetime(2026, 10, 19, tzinfo=UTC), due=datetime(2026, 10, 21, tzinfo=UTC))
    assert RouterAgent._note_context(note)["workspace_file"] is None
