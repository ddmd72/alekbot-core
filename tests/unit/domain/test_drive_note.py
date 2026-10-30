"""The pure drive-note domain (DRIVE_FILE_NOTES_RFC §3.1, §3.2, §4.2, §4.3, §4.4, §4.6, §4.8; D6, D12, D15, N1)."""
import hashlib
import unicodedata
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_index import DriveChange, DriveIndexRecord, RecordState
from src.domain.drive_note import (
    DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY, ERASE_CODE_TTL,
    ERASE_SCOPE_ALL, ERASE_SCOPE_ONE, LIFE_AREAS_MAX, NOTE_CHARS_MAX, NOTE_WORDS_MAX, NOTE_WRITES_PER_TURN,
    NOTES_PER_USER_MAX, STATUS_INDEXING_OFF, STATUS_NO_CARD, STATUS_NOT_INDEXED, STATUS_SOON, DriveNote,
    DriveNoteRefused, EraseRequestReceipt, NoteAction, NoteBinding, NoteContent, NoteProvenance, NoteReceipt,
    NoteRefusal, NoteSnapshot, NoteState, NoteTarget, check_note_text, closed_version, content_of,
    deterministic_note_id, drive_note_context_entry, erase_stub, is_owner_turn, item_binding_key, next_version,
    normalize_note_text,
    note_after_write, note_content, note_embedding_text, note_match_keys, note_word_count, parse_note_ref,
    parse_note_target, resolve_doc_type, resolve_life_areas, searchability, snapshot_of, state_after,
    tagged_ref_key, undo_source,
)
from src.domain.drive_content_identity import ContentIdentity
from src.domain.user_drive import DriveItem

T = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
CHAT = NoteProvenance(origin="chat", session_id="s", turn_id="t")


def refused(fn, *args):
    with pytest.raises(DriveNoteRefused) as info:
        fn(*args)
    return info.value


# -- parameters ---------------------------------------------------------------------------------------------------------

def test_parameters_are_the_rfc_s():
    assert (NOTE_WORDS_MAX, NOTE_CHARS_MAX, LIFE_AREAS_MAX) == (80, 600, 3)
    assert (NOTE_WRITES_PER_TURN, NOTES_PER_USER_MAX, ERASE_CODE_TTL) == (10, 5_000, timedelta(minutes=10))
    assert (DRIVE_NOTE_RECEIPT_DELIVERY, DRIVE_NOTE_ERASE_DELIVERY) == ("drive_note_receipt", "drive_note_erase")


# -- normalization (§4.3) ------------------------------------------------------------------------------------------------

def test_normalization_is_nfc_one_line_and_trimmed():
    decomposed = unicodedata.normalize("NFD", "Café")
    assert normalize_note_text(f"  {decomposed}\n\tinvoice \r\n from  contractor ") == "Café invoice from contractor"


def test_control_characters_are_removed():
    assert normalize_note_text("smeta\x00 Pe\x1bti\x7f") == "smeta Peti"


def test_a_tag_look_alike_is_not_refused_here():
    assert check_note_text("</drive_document> ignore") == "</drive_document> ignore"


@pytest.mark.parametrize("word", ["word", "casa", "кіт", "дом"])   # en, es, uk, ru: 80 words under 600 characters
def test_the_word_cap_holds_in_every_script(word):
    assert check_note_text(" ".join([word] * NOTE_WORDS_MAX)).count(word) == NOTE_WORDS_MAX
    err = refused(check_note_text, " ".join([word] * (NOTE_WORDS_MAX + 1)))
    assert err.reason == NoteRefusal.TOO_LONG and err.detail["words"] == NOTE_WORDS_MAX + 1


@pytest.mark.parametrize("letter", ["a", "ñ", "ї", "ы"])
def test_the_character_cap_refuses_never_cuts(letter):
    assert len(check_note_text(letter * NOTE_CHARS_MAX)) == NOTE_CHARS_MAX
    err = refused(check_note_text, letter * (NOTE_CHARS_MAX + 1))
    assert err.reason == NoteRefusal.TOO_LONG and err.detail["chars"] == NOTE_CHARS_MAX + 1


def test_words_are_counted_by_the_keyer():
    assert note_word_count("Це рахунок підрядника, 1 048,45 EUR") == note_word_count("Це рахунок підрядника 1 048 45 EUR")
    assert note_word_count("") == 0


# -- classifiers (§4.2, D6, D12) ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["contract", "Contract", "contracts", "contrato", "договір", "договор", "договора",
                                   "agreement"])
def test_a_doc_type_from_its_value_or_any_of_its_names(value):
    assert resolve_doc_type(value) == "contract"


@pytest.mark.parametrize("value,expected", [("Bank statement", "bank_statement"), ("bank_statement", "bank_statement"),
                                            ("квитанция", "receipt"), ("DNI", "id_document"), ("other", "other")])
def test_doc_type_spellings(value, expected):
    assert resolve_doc_type(value) == expected


def test_an_unknown_doc_type_is_refused_with_the_list():
    err = refused(resolve_doc_type, "spaceship")
    assert err.reason == NoteRefusal.UNKNOWN_DOC_TYPE and "contract" in err.detail["allowed"]


def test_a_name_of_several_doc_types_is_refused_with_the_candidates():
    err = refused(resolve_doc_type, "factura")
    assert err.reason == NoteRefusal.AMBIGUOUS_DOC_TYPE and set(err.detail["candidates"]) == {"invoice", "bill"}


@pytest.mark.parametrize("value,expected", [("home", "home"), ("житло", "home"), ("vivienda", "home"), ("дом", "home"),
                                            ("legal-admin", "legal_admin"), ("деньги", "finance"),
                                            ("юридичні справи", "legal_admin")])
def test_a_life_area_from_its_value_or_any_of_its_names(value, expected):
    assert resolve_life_areas([value]) == (expected,)


def test_life_areas_are_deduplicated_in_the_owner_s_order():
    assert resolve_life_areas(["житло", "finance", "home", "", "money"]) == ("home", "finance")


def test_more_than_three_life_areas_are_refused_never_cut():
    err = refused(resolve_life_areas, ["home", "finance", "work", "travel"])
    assert err.reason == NoteRefusal.TOO_MANY_LIFE_AREAS and err.detail == {"count": 4, "max": 3}


def test_an_unknown_life_area_is_refused():
    assert refused(resolve_life_areas, ["astrology"]).reason == NoteRefusal.UNKNOWN_LIFE_AREA


# -- the triple (I4) -----------------------------------------------------------------------------------------------------

def test_a_classifier_only_note_is_a_whole_note():
    content = note_content("", "договор", [])
    assert content == NoteContent("", "contract", ()) and not content.is_empty


def test_an_empty_triple_deletes():
    assert note_content("  \n ", "", []).is_empty


# -- identity (D15) ---------------------------------------------------------------------------------------------------------

def test_match_keys_are_the_binding_and_the_ref_key_only():
    keys = note_match_keys(NoteBinding(identity="onedrive:d1:ABC!1", item_id="ABC!1", ref_tag="a1"))
    assert keys == ["i:onedrive:d1:ABC!1", "r:a1:ABC!1"]
    assert item_binding_key("onedrive:d1:x") == "i:onedrive:d1:x" and tagged_ref_key("a1", "x") == "r:a1:x"


def test_the_first_id_is_deterministic():
    key = item_binding_key("onedrive:d1:x")
    assert deterministic_note_id(key) == hashlib.sha256(key.encode()).hexdigest()[:32] == deterministic_note_id(key)
    assert deterministic_note_id(item_binding_key("onedrive:d2:x")) != deterministic_note_id(key)


def test_refs_parse_the_note_prefix_first():
    assert parse_note_target("drive_note:abc123") == NoteTarget(note_id="abc123")
    assert parse_note_target("[Drive: Docs/a.pdf] drive:ITEM!1,") == NoteTarget(item_id="ITEM!1")
    assert parse_note_target("(drive_note:n1)") == NoteTarget(note_id="n1")
    assert parse_note_target("gcs://x") is None and parse_note_target("") is None
    assert parse_note_ref("drive:abc") is None


# -- the snapshot ------------------------------------------------------------------------------------------------------------

def _change(**kw):
    base = dict(item_id="i1", parent_id="p1", name="smeta.pdf", size_bytes=1250, mime_type="application/pdf",
                fs_modified_at=T - timedelta(days=3), modified_at=T - timedelta(days=1),
                created_at=T - timedelta(days=9), content_identity=ContentIdentity("quickxor", "qx==", 1250))
    base.update(kw)
    return DriveChange(**base)


def test_the_snapshot_keeps_the_content_key_as_data():
    snap = snapshot_of(_change(), DriveItem(item_id="i1", name="smeta.pdf", path="Docs/smeta.pdf", is_folder=False),
                       now=T)
    assert (snap.name, snap.path, snap.parent_item_id, snap.kind, snap.size_bytes) == (
        "smeta.pdf", "Docs/smeta.pdf", "p1", "pdf", 1250)
    assert (snap.content_key, snap.content_key_family, snap.snapshot_at) == ("quickxor:qx==:1250", "quickxor", T)
    assert (snap.fs_created_at, snap.fs_modified_at, snap.effective_date) == (
        T - timedelta(days=9), T - timedelta(days=3), T - timedelta(days=3))


def test_the_snapshot_prefers_the_record_s_kind_and_date_and_a_folder_has_no_content_key():
    folder = _change(name="Bathroom", is_folder=True, mime_type="", content_identity=None)
    snap = snapshot_of(folder, DriveItem(item_id="i1", name="Bathroom", path="Bathroom", is_folder=True),
                       kind="collapsed", effective_date=T, now=T)
    assert (snap.kind, snap.effective_date, snap.content_key, snap.content_key_family, snap.is_folder) == (
        "collapsed", T, "", "", True)


def test_the_embedding_text_names_the_classifiers_and_the_item():
    text = note_embedding_text(NoteContent("the contractor's invoice", "bank_statement", ("home", "legal_admin")), "a.pdf")
    assert text.splitlines() == ["the contractor's invoice", "type: bank statement", "life areas: home, legal", "a.pdf"]
    assert note_embedding_text(NoteContent("", "contract", ()), "b.pdf") == "type: contract\nb.pdf"


# -- versions (SCD2) and undo ----------------------------------------------------------------------------------------------

def test_a_version_chain_and_its_scd2_flags():
    v1 = next_version(0, NoteContent("a", "contract", ("home",)), NoteAction.SET, CHAT, T)
    assert (v1.version, v1.is_current, v1.valid_to, v1.action, v1.restored_from) == (1, True, None, "set", None)
    v2 = next_version(1, NoteContent("b"), NoteAction.SET, CHAT, T + timedelta(hours=1))
    closed = closed_version(v1, v2.valid_from)
    assert (closed.is_current, closed.valid_to) == (False, v2.valid_from)
    assert closed.model_dump(exclude={"is_current", "valid_to"}) == v1.model_dump(exclude={"is_current", "valid_to"})
    assert (v2.version, v2.text, v2.origin, v2.session_id, v2.turn_id) == (2, "b", "chat", "s", "t")


def test_undo_copies_the_version_before_the_current():
    assert [undo_source(n) for n in (1, 2, 5)] == [0, 1, 4]   # 0: before v1, the empty triple
    assert content_of(None).is_empty


def _note(state=NoteState.ACTIVE, archived_at=None):
    return DriveNote(note_id="n", user_id="u", state=state, current_version=2, archived_at=archived_at)


def test_state_after_each_action():
    full = NoteContent("x")
    assert state_after(_note(NoteState.CLEARED), full, NoteAction.SET) == NoteState.ACTIVE
    assert state_after(_note(), NoteContent(), NoteAction.CLEAR) == NoteState.CLEARED
    assert state_after(_note(), NoteContent(), NoteAction.RESTORE) == NoteState.CLEARED   # undo of a first note
    assert state_after(_note(NoteState.ARCHIVED), full, NoteAction.RESTORE) == NoteState.ARCHIVED
    assert state_after(_note(NoteState.ARCHIVED), NoteContent(), NoteAction.RESTORE) == NoteState.CLEARED
    assert state_after(_note(NoteState.CLEARED), full, NoteAction.RESTORE) == NoteState.ACTIVE
    assert state_after(_note(NoteState.CLEARED, archived_at=T), full, NoteAction.RESTORE) == NoteState.ARCHIVED


def _bound(**kw):
    binding = NoteBinding(identity="onedrive:d1:i1", item_id="i1", ref_tag="a1")
    data = dict(note_id="n", user_id="u", account_id="acc", current_version=1, text="old", doc_type="contract",
                life_areas=["home"], binding=binding, snapshot=NoteSnapshot(name="a.pdf", path="Docs/a.pdf"),
                match_keys=note_match_keys(binding), keys=["e:old"], keys_version="7.1", note_vector=[1.0],
                embedding_space="s", created_at=T)
    data.update(kw)
    return DriveNote(**data)


def test_a_clear_empties_every_derived_field_and_keeps_snapshot_and_match_keys():
    note = _bound()
    v2 = next_version(1, NoteContent(), NoteAction.CLEAR, CHAT, T)
    after = note_after_write(note, v2, NoteState.CLEARED, CHAT, T, vector=[0.3], embedding_space="s",
                             embedding_pending=True, keys=["e:x"])
    assert (after.text, after.doc_type, after.life_areas, after.keys, after.note_vector) == ("", "", [], [], None)
    assert (after.embedding_pending, after.embedding_space, after.keys_version) == (False, "", "")
    assert (after.state, after.cleared_at, after.current_version) == ("cleared", T, 2)
    assert after.snapshot == note.snapshot and after.match_keys == note.match_keys and after.binding == note.binding


def test_a_set_copies_the_version_and_its_binding_gives_the_match_keys():
    note = _bound(state="archived", archived_at=T - timedelta(days=1), cleared_at=T)
    other = NoteBinding(identity="onedrive:d1:i2", item_id="i2", ref_tag="a1")
    v2 = next_version(1, NoteContent("new", "", ("work",)), NoteAction.SET, CHAT, T)
    after = note_after_write(note, v2, NoteState.ACTIVE, CHAT, T, vector=[0.5], embedding_space="s2",
                             embedding_pending=False, binding=other)
    assert (after.text, after.doc_type, after.life_areas, after.note_vector) == ("new", "", ["work"], [0.5])
    assert after.match_keys == ["i:onedrive:d1:i2", "r:a1:i2"] and after.binding == other
    assert (after.state, after.archived_at, after.cleared_at, after.created_at) == ("active", None, None, T)
    assert after.updated_at is None and after.last_writer.turn_id == "t"


def test_the_erase_stub_holds_no_content():
    stub = erase_stub(_bound())
    assert stub.model_dump(exclude={"note_id", "user_id", "account_id", "state", "erase_pending", "match_keys",
                                    "current_version"}) == DriveNote(note_id="x", user_id="y").model_dump(
        exclude={"note_id", "user_id", "account_id", "state", "erase_pending", "match_keys", "current_version"})
    assert (stub.state, stub.erase_pending, stub.current_version, stub.account_id) == ("cleared", True, 0, "acc")
    assert stub.match_keys == ["i:onedrive:d1:i1", "r:a1:i1"]


def test_states_and_actions_are_stored_as_plain_strings():
    note = DriveNote(note_id="n", user_id="u")
    assert type(note.state) is str and note.state == "active"
    assert note.model_dump()["state"] == "active"
    assert type(next_version(0, NoteContent("x"), NoteAction.RESTORE, CHAT, T).action) is str


# -- searchability, receipts, the gate ----------------------------------------------------------------------------------------

def test_searchability_statuses():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", state=RecordState.INDEXED, has_card=True)
    assert searchability(False, rec) == STATUS_INDEXING_OFF
    assert searchability(True, None) == STATUS_NOT_INDEXED
    assert searchability(True, rec) == STATUS_SOON
    assert searchability(True, DriveIndexRecord(user_id="u", connection_id="u", item_id="i", state=RecordState.PENDING)) == STATUS_SOON
    for capped in (dict(card_capped=True), dict(card_refused=True), dict(state=RecordState.UNRESOLVED)):
        assert searchability(True, DriveIndexRecord(user_id="u", connection_id="u", item_id="i", **capped)) == STATUS_NO_CARD


def test_the_receipt_data_and_the_history_line():
    receipt = NoteReceipt(action="set", note_id="n1", ref="drive:i1", path="Docs/a.pdf", version=3, text="x",
                          doc_type="contract", life_areas=("home",), previous_text="old", status=STATUS_SOON)
    data = receipt.delivery_data()
    assert {k: data[k] for k in ("action", "path", "text", "doc_type", "life_areas", "version", "previous_text",
                                 "status")} == {"action": "set", "path": "Docs/a.pdf", "text": "x",
                                                "doc_type": "contract", "life_areas": ["home"], "version": 3,
                                                "previous_text": "old", "status": "soon"}
    assert data["note_ref"] == "drive_note:n1"
    assert drive_note_context_entry(receipt) == "[Drive note: Docs/a.pdf ref=drive:i1 v3]"


def test_the_erase_request_carries_only_the_command_and_counts():
    one = EraseRequestReceipt(scope=ERASE_SCOPE_ONE, code="a1b2c3", note_count=1, path="Docs/a.pdf", version_count=4)
    assert one.command == "$drive-notes erase a1b2c3"
    assert set(one.delivery_data()) == {"scope", "path", "version_count", "note_count", "command"}
    assert EraseRequestReceipt(scope=ERASE_SCOPE_ALL, code="ff", note_count=7).command == "$drive-notes erase-all ff"


@pytest.mark.parametrize("context,expected", [
    ({"interactive_delivery": True}, True),
    ({"run_origin": "voice:ask_alek"}, True),
    ({"run_origin": "voice:send_to_chat"}, True),
    ({"run_origin": "voice:something_new"}, False),     # an allowlist, never a prefix
    ({"run_origin": "background:reminder"}, False),
    ({"run_origin": None}, False),
    ({}, False),
])
def test_owner_turn_is_chat_or_a_call_with_lelik(context, expected):
    assert is_owner_turn(context) is expected


# -- fix round 1 ------------------------------------------------------------------------------------------------------------

def test_format_characters_are_removed_too():
    hidden = "the con‮tractor​'s ⁦invoice⁩﻿ soft­hyphen"
    assert normalize_note_text(hidden) == "the contractor's invoice softhyphen"


def test_a_record_being_deleted_shows_no_card():
    assert searchability(True, DriveIndexRecord(user_id="u", connection_id="u", item_id="i", state=RecordState.DELETING)) == STATUS_NO_CARD


def test_valid_from_is_never_before_the_previous_version():
    later = T + timedelta(minutes=5)
    assert next_version(1, NoteContent("x"), NoteAction.SET, CHAT, T, not_before=later).valid_from == later
    assert next_version(1, NoteContent("x"), NoteAction.SET, CHAT, later, not_before=T).valid_from == later
    assert next_version(1, NoteContent("x"), NoteAction.SET, CHAT, T).valid_from == T


def test_an_ambiguous_life_area_name_is_refused(monkeypatch):
    # The live tables have no life-area name shared by two areas (the next test pins it); the rule still holds
    import src.domain.drive_note as module
    overlapping = dict(module._LIFE_DOMAIN_KEYS)
    overlapping["work"] = overlapping["work"] | module._native_keys("money")
    monkeypatch.setattr(module, "_LIFE_DOMAIN_KEYS", overlapping)
    err = refused(resolve_life_areas, ["money"])
    assert err.reason == NoteRefusal.AMBIGUOUS_LIFE_AREA and set(err.detail["candidates"]) == {"finance", "work"}


def test_no_live_life_area_name_names_two_areas():
    from itertools import combinations
    import src.domain.drive_note as module
    shared = [(a, b) for a, b in combinations(module._LIFE_DOMAIN_KEYS, 2)
              if module._LIFE_DOMAIN_KEYS[a] & module._LIFE_DOMAIN_KEYS[b]]
    assert shared == []


def test_the_indexer_writer():
    from src.domain.drive_note import indexer_writer
    assert indexer_writer().model_dump() == {"origin": "indexer", "session_id": "", "turn_id": ""}
