"""A reminder stored while the `untrusted` flag existed still loads: the adapter builds the note field by field, so the
stored field is ignored. The store kit's Firestore fake and port."""
from datetime import timedelta

from tests.unit.adapters.test_agent_note_store_kit import DAD, DUE, T0, notes  # noqa: F401


async def test_a_record_with_the_old_untrusted_field_loads_as_an_ordinary_note(notes):  # noqa: F811
    notes.seed("n1", user_id="u1", created_at=T0, due=DUE, last_fired=None, recurrence=None, untrusted=True, **DAD)
    note = await notes.port.get_note("u1", "n1")
    assert note is not None and (note.text, note.instruction) == (DAD["text"], DAD["instruction"])
    assert not hasattr(note, "untrusted")
    [listed] = await notes.port.list_active_notes("u1", as_of=DUE - timedelta(days=2))
    assert listed.note_id == "n1"
