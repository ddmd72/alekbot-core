"""workspace_file defaults on the reminder records (AGENT_WORKSPACE_RFC §8.3). The store's handling of the field
(written only when set, cleared by the clear flag, read back, absent reads as None) is asserted in
test_agent_note_store_kit.py."""
from datetime import datetime, timedelta, timezone

from src.domain.agent_note import AgentNote, NoteUpdate

USER = "user-abc123"
NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
DUE = NOW + timedelta(days=1)


def test_domain_defaults():
    note = AgentNote(note_id="1", user_id=USER, text="x", instruction="x", created_at=NOW, due=DUE)
    assert note.workspace_file is None
    update = NoteUpdate(note_id="1", user_id=USER)
    assert (update.workspace_file, update.clear_workspace_file) == (None, False)
