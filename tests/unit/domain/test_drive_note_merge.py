"""The note merge (DRIVE_FILE_NOTES_RFC §5.1, §5.5, D9, D15): `with_owner_note` sets, changes and clears the record's
copy idempotently; only an active note bound to this item on the current drive gives a view; the planner's `_build`
flags a record it creates and carries the copy across an observation; `planned()` recomputes the about keys from it;
the changed card is due in class 0; the watermark's next start. Synthetic notes only."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.domain.drive_card import card_hash
from src.domain.drive_enrich_plan import default_context, planned
from src.domain.drive_index import (
    DriveChange, DriveIndexRecord, DriveSyncState, EpochPhase, Priority, RecordState, Scope,
)
from src.domain.drive_index_planner import PlanContext, plan_observation, schedule
from src.domain.drive_keys import note_keys
from src.domain.drive_index import KEYS_VERSION
from src.domain.drive_note import (
    NOTE_KEYS_VERSION, ONEDRIVE_PROVIDER_KEY, DriveNote, NoteBinding, NoteContent, NoteSnapshot, NoteState, binding_key, erase_stub,
    note_match_keys,
)
from src.domain.drive_note_merge import (
    NOTE_MERGE_VERSION, NOTE_STORE_KEYS_VERSION, NOTE_WATERMARK_SKEW_S, NoteView, merged, note_store_keys, note_view,
    notes_query_start,
    record_binding_key, with_owner_note,
)

T = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
VIEW = NoteView(note_id="n1", version=2, text="the contractor's invoice", doc_type="contract", life_areas=("home",))


def _note(state: str = "active", drive_id: str = "d1", provider: str = ONEDRIVE_PROVIDER_KEY, **kw) -> DriveNote:
    binding = NoteBinding(provider_key=provider, drive_id=drive_id, item_id="i1", bound_at=T)
    base = dict(note_id="n1", user_id="u", state=state, current_version=2, text="the contractor's invoice",
                doc_type="contract", life_areas=["home"], binding=binding, match_keys=note_match_keys(binding),
                snapshot=NoteSnapshot(name="a.pdf"))
    base.update(kw)
    return DriveNote(**base)


def _rec(**kw) -> DriveIndexRecord:
    return DriveIndexRecord(user_id="u", connection_id="u", item_id="i1", name="a.pdf", kind="pdf", **kw)


def test_set_change_and_clear_are_idempotent():
    rec = _rec()
    once = with_owner_note(rec, VIEW)
    assert with_owner_note(once, VIEW) == once and merged(once, VIEW)
    assert (once.owner_note_id, once.owner_note_version, once.owner_note_text, once.owner_doc_type,
            once.owner_life_areas, once.owner_note_merge_version) == ("n1", 2, "the contractor's invoice", "contract",
                                                                     ("home",), NOTE_MERGE_VERSION)
    changed = with_owner_note(once, replace(VIEW, version=3, text="the contractor's final invoice"))
    assert changed.owner_note_version == 3 and changed.owner_note_text == "the contractor's final invoice"
    cleared = with_owner_note(changed, None)
    assert cleared == rec and with_owner_note(cleared, None) == cleared and merged(cleared, None)


def test_the_merge_touches_only_the_owner_fields():
    rec = _rec(effective_date=T, card_accuracy=88, profile_state="current")
    out = with_owner_note(rec, VIEW)
    assert (out.effective_date, out.card_accuracy, out.profile_state, out.profile) == (T, 88, "current", None)


def test_only_an_active_note_on_the_current_drive_gives_a_view():
    assert note_view(_note(), drive_id="d1") == VIEW
    assert note_view(_note("archived"), drive_id="d1") is None            # D9
    assert note_view(_note("cleared", text="", doc_type="", life_areas=[]), drive_id="d1") is None
    assert note_view(_note(), drive_id="d2") is None                      # D15/D16: dormant on another drive
    assert note_view(_note(provider="other"), drive_id="d1") is None
    assert note_view(erase_stub(_note()), drive_id="d1") is None
    assert note_view(_note(), drive_id="") is None
    assert note_view(None, drive_id="d1") is None


def _ctx() -> PlanContext:
    return PlanContext(user_id="u", root_item_id="root", epoch=1, phase=EpochPhase.IDLE, now=T,
                       place_of=lambda lat, lon: None)


def _change(name: str = "a.pdf") -> DriveChange:
    return DriveChange(item_id="i1", parent_id="root", name=name, mime_type="application/pdf", size_bytes=10,
                       content_version="c1", modified_at=T - timedelta(days=1), fs_modified_at=T - timedelta(days=1))


def test_build_flags_a_created_record_and_carries_the_copy_across_an_observation():
    created = plan_observation(_change(), None, None, None, _ctx()).upsert
    assert created is not None and created.note_lookup_due
    noted = replace(with_owner_note(created, VIEW), note_lookup_due=False)
    renamed = plan_observation(_change("b.pdf"), noted, None, None, _ctx()).upsert
    assert renamed is not None and not renamed.note_lookup_due
    assert (renamed.owner_note_id, renamed.owner_note_text, renamed.owner_doc_type) == \
        ("n1", "the contractor's invoice", "contract")


def test_planned_recomputes_the_about_keys_from_the_copy():
    rec = with_owner_note(_rec(), VIEW)
    keys = planned(rec, default_context(T)).about_keys
    assert set(note_keys(VIEW.text, VIEW.doc_type, VIEW.life_areas)) <= set(keys)
    assert not set(note_keys(VIEW.text)) & set(planned(with_owner_note(rec, None), default_context(T)).about_keys)


def test_a_carded_record_is_due_for_its_card_in_class_0():
    base = schedule(_rec(scope=Scope.NORMAL, state=RecordState.INDEXED), T, first_seen=False)
    carded = replace(base, has_card=True, card_hash=card_hash(base), due_at=None, priority=None)
    noted = with_owner_note(carded, VIEW)
    assert card_hash(noted) != carded.card_hash
    due = schedule(noted, T, first_seen=False)
    assert due.due_at == T and due.priority == int(Priority.EDIT)


def test_note_store_keys():
    keys, version = note_store_keys(NoteContent("the contractor's invoice", "contract", ("home",)), "Bathroom.pdf")
    assert set(note_keys("the contractor's invoice", "contract", ("home",))) <= set(keys)
    assert "w:bathroom" in keys and version == NOTE_STORE_KEYS_VERSION == f"{KEYS_VERSION}.{NOTE_KEYS_VERSION}"
    assert note_store_keys(NoteContent(), "Bathroom.pdf") == ([], "")


def test_notes_query_start():
    state = DriveSyncState(user_id="u", connection_id="u")
    assert notes_query_start(state) == (None, "")
    paging = replace(state, notes_applied_at=T, notes_applied_id="n9", notes_paging=True)
    assert notes_query_start(paging) == (T, "n9")
    skew = timedelta(seconds=NOTE_WATERMARK_SKEW_S)
    recent = replace(state, notes_applied_at=T, notes_applied_id="n9", notes_scanned_at=T + timedelta(seconds=10))
    assert notes_query_start(recent) == (T + timedelta(seconds=10) - skew, "")
    old = replace(state, notes_applied_at=T, notes_applied_id="n9", notes_scanned_at=T + timedelta(hours=1))
    assert notes_query_start(old) == (T, "n9")   # the skew window is long past: strictly after the pair
    no_end = replace(state, notes_applied_at=T, notes_applied_id="n9")
    assert notes_query_start(no_end) == (T - skew, "")


def test_note_state_values_are_plain_strings():
    assert NoteState.ACTIVE.value == "active" and _note().state == "active"
