"""U2-3: NoteView.of (specialist RFC §9.2, design §4.1)."""
from src.domain.drive_note import DriveNote, NoteBinding, NoteSnapshot, NoteState
from src.domain.drive_note_view import NoteView


def _note(**kw):
    data = dict(note_id="n1", user_id="u", current_version=3, text="the contractor's invoice", doc_type="contract",
                life_areas=["home"], binding=NoteBinding(identity="fake:d1:i1", item_id="i1", ref_tag="a1"),
                snapshot=NoteSnapshot(name="a.pdf", path="Docs/a.pdf"))
    data.update(kw)
    return DriveNote(**data)


def test_active_note():
    view = NoteView.of(_note())
    assert (view.note_id, view.ref, view.note_ref, view.path) == ("n1", "drive:a1:i1", "drive_note:n1", "Docs/a.pdf")
    assert (view.version, view.state, view.text, view.doc_type, view.life_areas) == (
        3, "active", "the contractor's invoice", "contract", ("home",))
    assert not view.cleared and not view.archived


def test_cleared_note():
    view = NoteView.of(_note(state=NoteState.CLEARED, text="", doc_type="", life_areas=[]))
    assert view.cleared and not view.archived and view.text == "" and view.life_areas == ()


def test_archived_note():
    view = NoteView.of(_note(state=NoteState.ARCHIVED))
    assert view.archived and not view.cleared and view.state == "archived"


def test_a_stub_has_no_item_ref_and_no_path():
    view = NoteView.of(DriveNote(note_id="n2", user_id="u", state=NoteState.CLEARED, binding=None, snapshot=None))
    assert (view.ref, view.path, view.note_ref) == ("", "", "drive_note:n2")
