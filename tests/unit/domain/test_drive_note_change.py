"""U2-3: NoteChange constructors and apply_change (specialist RFC §9.2, design §4.1, Δ5)."""
import pytest

from src.domain.drive_note import NOTE_WORDS_MAX, DriveNoteRefused, NoteContent, NoteRefusal
from src.domain.drive_note_change import NoteChange, apply_change
from src.domain.drive_note_change_kind import ADDITIVE_KINDS, NoteChangeKind


def _refused(call):
    with pytest.raises(DriveNoteRefused) as info:
        call()
    return info.value


def test_text_constructors_normalize_and_refuse_empty_and_long():
    assert NoteChange.append_text("  the contractor's \n invoice ").text == "the contractor's invoice"
    assert NoteChange.replace_text("the contractor's invoice").kind == NoteChangeKind.REPLACE_TEXT
    for make in (NoteChange.append_text, NoteChange.replace_text):
        assert _refused(lambda: make("   ")).reason == NoteRefusal.EMPTY_CHANGE
        assert _refused(lambda: make("word " * (NOTE_WORDS_MAX + 1))).reason == NoteRefusal.TOO_LONG


def test_type_constructors():
    assert NoteChange.set_type("contract").doc_type == "contract"
    assert _refused(lambda: NoteChange.set_type("")).reason == NoteRefusal.EMPTY_CHANGE
    assert _refused(lambda: NoteChange.set_type("no such type")).reason == NoteRefusal.UNKNOWN_DOC_TYPE
    assert NoteChange.clear_type().kind == NoteChangeKind.CLEAR_TYPE


def test_area_constructors():
    assert NoteChange.add_areas(["home", "home"]).life_areas == ("home",)
    assert NoteChange.remove_areas(["home"]).kind == NoteChangeKind.REMOVE_AREAS
    for make in (NoteChange.add_areas, NoteChange.remove_areas):
        assert _refused(lambda: make(())).reason == NoteRefusal.EMPTY_CHANGE
        assert _refused(lambda: make(["no such area"])).reason == NoteRefusal.UNKNOWN_LIFE_AREA
    assert _refused(lambda: NoteChange.add_areas(["home", "legal_admin", "health", "finance"])).reason == (
        NoteRefusal.TOO_MANY_LIFE_AREAS)


def test_additive_kinds_may_create():
    assert {k for k in NoteChangeKind if NoteChange(k).additive} == set(ADDITIVE_KINDS)
    assert not NoteChange.replace_text("x").additive and not NoteChange.clear_type().additive


def test_append_joins_with_one_space():
    after = apply_change(NoteContent("the contractor's invoice", "contract", ("home",)), NoteChange.append_text("for the bathroom"))
    assert after == NoteContent("the contractor's invoice for the bathroom", "contract", ("home",))
    assert apply_change(NoteContent("", "contract", ()), NoteChange.append_text("first")).text == "first"


def test_append_overflow_names_the_current_length():
    current = NoteContent("word " * (NOTE_WORDS_MAX - 1), "", ())
    refused = _refused(lambda: apply_change(current, NoteChange.append_text("one two three")))
    assert refused.reason == NoteRefusal.TOO_LONG
    assert refused.detail["current_words"] == NOTE_WORDS_MAX - 1 and refused.detail["words"] == NOTE_WORDS_MAX + 2
    assert str(NOTE_WORDS_MAX - 1) in str(refused)


def test_replace_and_type_changes_keep_the_other_fields():
    base = NoteContent("old", "contract", ("home",))
    assert apply_change(base, NoteChange.replace_text("new")) == NoteContent("new", "contract", ("home",))
    assert apply_change(base, NoteChange.set_type("bank_statement")).doc_type == "bank_statement"
    assert apply_change(base, NoteChange.clear_type()) == NoteContent("old", "", ("home",))


def test_add_areas_keeps_order_and_refuses_a_fourth():
    base = NoteContent("old", "", ("home", "legal_admin"))
    assert apply_change(base, NoteChange.add_areas(["home"])).life_areas == ("home", "legal_admin")
    full = NoteContent("old", "", ("home", "legal_admin", "health"))
    assert _refused(lambda: apply_change(full, NoteChange.add_areas(["finance"]))).reason == (
        NoteRefusal.TOO_MANY_LIFE_AREAS)


def test_remove_and_clear_of_an_absent_value_are_no_ops():
    base = NoteContent("old", "", ("home",))
    assert apply_change(base, NoteChange.clear_type()) == base
    assert apply_change(base, NoteChange.remove_areas(["legal_admin"])) == base
    assert apply_change(base, NoteChange.remove_areas(["home"])) == NoteContent("old", "", ())


def test_a_result_with_an_empty_triple_is_refused():
    for content, change in (
            (NoteContent("", "contract", ()), NoteChange.clear_type()),
            (NoteContent("", "", ("home",)), NoteChange.remove_areas(["home"]))):
        refused = _refused(lambda: apply_change(content, change))
        assert refused.reason == NoteRefusal.EMPTY_CHANGE and "delete_note" in str(refused)
