"""A drive note's connection binding (MULTI_DRIVE_RFC §11.10; design §1.8): the `c:` key, the match keys of a drive
binding (no `i:`, no `r:`), `is_drive_note` (a stub too), `item_ref` "" and `note_ref_tag` of a drive note."""
import pytest

from src.domain.drive_note import (
    CONNECTION_KEY_PREFIX, DriveNote, NoteBinding, NoteTarget, connection_binding_key, deterministic_note_id,
    drive_note_connection_id, erase_stub, is_drive_note, item_ref, note_match_keys, note_ref_tag, on_connection,
)

DRIVE_BINDING = NoteBinding(item_id="", connection_id="conn-2", ref_tag="b2")
ITEM_BINDING = NoteBinding(item_id="item-7", identity="fake_b:d2:item-7", ref_tag="b2")


def note(binding, **over):
    return DriveNote(note_id="n1", user_id="usr-1", text="Acme work files: R&D, Tom's plan", binding=binding,
                     match_keys=note_match_keys(binding), **over)


def test_the_connection_key():
    assert CONNECTION_KEY_PREFIX == "c:"
    assert connection_binding_key("conn-2") == "c:conn-2"
    for bad in ("", "usr:conn", "a/b"):
        with pytest.raises(ValueError):
            connection_binding_key(bad)


def test_a_drive_binding_matches_by_its_connection_only():
    assert note_match_keys(DRIVE_BINDING) == ["c:conn-2"]
    assert note_match_keys(ITEM_BINDING) == ["i:fake_b:d2:item-7", "r:b2:item-7"]


def test_is_drive_note_and_its_connection():
    drive, item = note(DRIVE_BINDING), note(ITEM_BINDING)
    assert is_drive_note(drive) and drive_note_connection_id(drive) == "conn-2"
    assert not is_drive_note(item) and drive_note_connection_id(item) == ""


def test_an_erase_stub_of_a_drive_note_is_still_one():
    stub = erase_stub(note(DRIVE_BINDING))
    assert stub.binding is None and is_drive_note(stub) and drive_note_connection_id(stub) == "conn-2"
    assert not is_drive_note(erase_stub(note(ITEM_BINDING)))


def test_a_drive_note_has_no_item_ref_but_its_connections_tag():
    drive = note(DRIVE_BINDING)
    assert item_ref(drive) == ""
    assert note_ref_tag(drive) == "b2" and on_connection(drive, "b2")
    assert item_ref(note(ITEM_BINDING)) == "drive:b2:item-7"


def test_the_lineage_id_of_a_drive_note_is_deterministic():
    assert deterministic_note_id(connection_binding_key("conn-2")) == deterministic_note_id("c:conn-2")
    assert deterministic_note_id("c:conn-2") != deterministic_note_id("c:conn-1")


def test_a_note_target_can_name_a_connection():
    target = NoteTarget(connection_id="conn-2")
    assert (target.note_id, target.item_id, target.connection_id) == ("", "", "conn-2")
