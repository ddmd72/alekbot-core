"""Slice 1b part D, domain: tagged refs in labels, outcomes and passages; the gate and the specialist's lines over a
connection; notes bound by identity and the connection's tag (MULTI_DRIVE_RFC §7.2, §9, §11.6)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_connection_status import DriveConnectionStatus
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_excerpt_render import _ids
from src.domain.drive_excerpt_request import NOT_A_REF, excerpt_request, range_request
from src.domain.drive_excerpt_result import ExcerptResult
from src.domain.drive_index import DriveChange, DriveIndexRecord, DriveSyncState
from src.domain.drive_line import DriveLine
from src.domain.drive_note import (
    DriveNote, NoteBinding, NoteSnapshot, binding_identity, item_ref, note_match_keys, note_ref_tag, on_connection,
    parse_note_target, snapshot_of,
)
from src.domain.drive_note_lifecycle import audit_candidates, refreshed_snapshot, snapshot_from_record, snapshot_stale
from src.domain.drive_note_merge import note_view
from src.domain.drive_note_render import note_hit
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome import DriveOutcome
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_search import STATUS_DISABLED, STATUS_DISCONNECTED, STATUS_OK, make_card, search_gate
from src.domain.drive_search_line import SearchLine, search_line
from src.domain.user_drive import DriveItem, drive_context_entry, drive_label, item_ref as drive_item_ref
from src.domain.workspace import parse_workspace_path
from src.domain.exceptions import WorkspaceRefused

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
FILE = DriveItem(item_id="item-1", name="report.pdf", path="Docs/report.pdf", is_folder=False, size_bytes=2048)
FOLDER = DriveItem(item_id="item-2", name="Docs", path="Docs", is_folder=True)


def _conn(state=ConnectionState.ACTIVE, identity="fake_a:drive-1", indexing=True) -> DriveConnection:
    return DriveConnection(connection_id="conn-1", user_id="usr-1", account_id="acc-1", credential_id="cred-1",
                           kind="fake_a", drive_identity=identity, ref_tag="a1", indexing_enabled=indexing,
                           state=state, created_at=NOW,
                           disconnected_at=NOW if state is ConnectionState.DISCONNECTED else None)


class TestLabelsAndOutcomes:
    def test_labels_and_history_entries_carry_the_connection_tag(self):
        assert drive_item_ref(FILE, "a1") == "drive:a1:item-1"
        assert drive_label(FILE, "a1") == "[Drive: Docs/report.pdf (2KB) ref=drive:a1:item-1]"
        assert drive_label(FOLDER, "b2") == "[Drive folder: Docs/ ref=drive:b2:item-2]"
        assert drive_context_entry(FILE, "a1") == {"path": "Docs/report.pdf", "ref": "drive:a1:item-1"}

    def test_a_ref_needs_a_tag(self):
        with pytest.raises(ValueError):
            drive_label(FILE, "")

    def test_an_outcome_tags_its_items(self):
        outcome = DriveOutcome(op=DriveOp.LIST, cls=OutcomeClass.OK, text="", success=True, items=(FILE,),
                               ref_tag="a1")
        assert outcome.drive_context() == [{"path": "Docs/report.pdf", "ref": "drive:a1:item-1"}]
        assert outcome.ref_of(FILE) == "drive:a1:item-1"
        assert outcome.label_of(FILE).endswith("ref=drive:a1:item-1]")

    def test_an_outcome_with_items_and_no_tag_is_refused(self):
        with pytest.raises(ValueError):
            DriveOutcome(op=DriveOp.LIST, cls=OutcomeClass.OK, text="", success=True, items=(FILE,))
        assert DriveOutcome(op=DriveOp.SEARCH, cls=OutcomeClass.OK, text="", success=True).drive_context() == []


class TestPassagesRequests:
    def test_a_head_line_id_keeps_its_tag_and_drops_the_numbers(self):
        request = excerpt_request("what is due", file_refs=["drive:a1:item-1:3-5", "drive:a1:item-2:4"])
        assert request.file_refs == ("drive:a1:item-1", "drive:a1:item-2")
        assert range_request("[id drive:a1:item-1:2]", "1-2").file_ref == "drive:a1:item-1"

    def test_an_item_id_with_a_colon_survives(self):
        assert excerpt_request("q", file_refs="drive:a1:x:y").file_refs == ("drive:a1:x:y",)

    def test_an_untagged_ref_is_kept_as_given_for_the_stale_line(self):
        assert excerpt_request("q", file_refs="drive:ITEM-1:3").file_refs == ("drive:ITEM-1",)

    def test_a_non_ref_is_refused(self):
        with pytest.raises(ValueError, match="not a drive: ref"):
            excerpt_request("q", file_refs="report.pdf")
        assert "{value}" in NOT_A_REF

    def test_repeated_passage_ids_carry_the_result_tag(self):
        result = ExcerptResult(status=STATUS_OK, request=excerpt_request("q"), repeated=(("item-1", 1, 0),),
                               ref_tag="a1")
        assert _ids(result) == "drive:a1:item-1:1"


class TestGateAndLines:
    def test_no_connection_is_disconnected_and_the_switch_off_is_disabled(self):
        assert search_gate(True, None, None, NOW).status == STATUS_DISCONNECTED
        assert search_gate(False, None, None, NOW).status == STATUS_DISCONNECTED   # no switch to read
        assert search_line(False, None, None, NOW) is SearchLine.ON              # the drive line says it
        assert search_gate(False, None, _conn(), NOW).status == STATUS_DISABLED

    def test_a_disconnected_connection_is_disconnected(self):
        assert search_gate(True, None, _conn(ConnectionState.DISCONNECTED), NOW).status == STATUS_DISCONNECTED

    def test_needs_reconnect_still_searches_and_says_so(self):
        gate = search_gate(True, DriveSyncState(user_id="usr-1"), _conn(ConnectionState.NEEDS_RECONNECT), NOW)
        assert gate.status == STATUS_OK and gate.reconnect_needed

    def test_a_state_of_another_drive_identity_hides_the_index(self):
        state = DriveSyncState(user_id="usr-1", drive_identity="fake_a:drive-0")
        gate = search_gate(True, state, _conn(identity="fake_a:drive-1"), NOW)
        assert gate.status == STATUS_DISCONNECTED and gate.other_drive
        same = search_gate(True, DriveSyncState(user_id="usr-1", drive_identity="fake_a:drive-1"), _conn(), NOW)
        assert same.status == STATUS_OK and not same.other_drive

    def test_the_drive_line_reads_the_status(self):
        assert DriveLine.of(DriveConnectionStatus.of(None)) is DriveLine.NOT_CONNECTED
        assert DriveLine.of(DriveConnectionStatus.of(_conn())) is DriveLine.CONNECTED
        assert DriveLine.of(DriveConnectionStatus.of(_conn(ConnectionState.NEEDS_RECONNECT))) \
            is DriveLine.RECONNECT_NEEDED

    def test_the_search_line_over_a_connection(self):
        assert search_line(False, None, _conn(indexing=False), NOW) is SearchLine.OFF
        assert search_line(True, None, None, NOW) is SearchLine.ON
        assert search_line(True, None, _conn(), NOW) is SearchLine.BUILDING

    def test_a_card_ref_carries_the_tag(self):
        rec = DriveIndexRecord(user_id="usr-1", item_id="item-1", name="report.pdf")
        assert make_card(rec, "report.pdf", "", "n0nce000", "a1").ref == "drive:a1:item-1"


class TestOtherPrefixTests:
    def test_the_workspace_refuses_a_tagged_drive_ref(self):
        with pytest.raises(WorkspaceRefused) as refused:
            parse_workspace_path("drive:a1:item-1")
        assert refused.value.reason == "wrong_store"


def _note(**kw) -> DriveNote:
    return DriveNote(note_id="n1", user_id="usr-1", **kw)


class TestNoteBinding:
    BINDING = NoteBinding(identity="fake_a:drive-1:item-1", item_id="item-1", ref_tag="a1")

    def test_match_keys_are_identity_and_tagged_ref(self):
        assert note_match_keys(self.BINDING) == ["i:fake_a:drive-1:item-1", "r:a1:item-1"]

    def test_a_binding_without_identity_or_tag_has_no_keys(self):
        with pytest.raises(ValueError):
            note_match_keys(NoteBinding(item_id="item-1"))

    def test_binding_fields_come_first(self):
        note = _note(binding=self.BINDING, match_keys=["i:other", "r:b2:item-1"])
        assert binding_identity(note) == "fake_a:drive-1:item-1"
        assert note_ref_tag(note) == "a1"
        assert item_ref(note) == "drive:a1:item-1"

    def test_a_note_stored_before_the_connections_reads_its_match_keys(self):
        # the old binding's provider_key / drive_id are ignored on read; the cut-over added the tagged key
        note = DriveNote.model_validate({
            "note_id": "n1", "user_id": "usr-1",
            "binding": {"provider_key": "p", "drive_id": "d", "item_id": "item-1"},
            "match_keys": ["i:p:d:item-1", "r:item-1", "r:a1:item-1"]})
        assert binding_identity(note) == "p:d:item-1"
        assert note_ref_tag(note) == "a1"
        assert on_connection(note, "a1") and not on_connection(note, "b2") and not on_connection(note, "")

    def test_an_untagged_ref_key_gives_no_tag(self):
        note = _note(binding=NoteBinding(item_id="item-1"), match_keys=["i:p:d:item-1", "r:item-1"])
        assert note_ref_tag(note) == "" and item_ref(note) == ""

    def test_an_erase_stub_keeps_its_keys(self):
        stub = _note(match_keys=["i:fake_a:drive-1:item-1", "r:a1:item-1"], erase_pending=True)
        assert binding_identity(stub) == "fake_a:drive-1:item-1" and note_ref_tag(stub) == "a1"
        assert item_ref(stub) == ""

    def test_targets(self):
        assert parse_note_target("drive:a1:item-1").item_id == "item-1"
        assert parse_note_target("ref=drive:a1:item-1]").tag == "a1"
        untagged = parse_note_target("drive:ITEM-1")
        assert untagged.item_id == "ITEM-1" and untagged.tag == ""
        assert parse_note_target("drive_note:abc").note_id == "abc"
        assert parse_note_target("report.pdf") is None

    def test_the_snapshot_takes_the_content_identity(self):
        change = DriveChange(item_id="item-1", name="report.pdf", size_bytes=3,
                             content_identity=ContentIdentity("hashfam", "d1", 3))
        snapshot = snapshot_of(change, FILE, now=NOW)
        assert (snapshot.content_key, snapshot.content_key_family) == ("hashfam:d1:3", "hashfam")
        bare = snapshot_of(DriveChange(item_id="item-1", name="report.pdf"), FILE, now=NOW)
        assert (bare.content_key, bare.content_key_family) == ("", "")


class TestNoteRulesByConnection:
    def _active(self, tag: str, seen=None) -> DriveNote:
        return _note(state="active", text="noted", current_version=1,
                     binding=NoteBinding(identity="fake_a:drive-1:item-1", item_id="item-1", ref_tag=tag),
                     snapshot=NoteSnapshot(snapshot_at=seen))

    def test_the_merge_view_is_the_connections_notes_only(self):
        assert note_view(self._active("a1"), ref_tag="a1").text == "noted"
        assert note_view(self._active("b2"), ref_tag="a1") is None

    def test_the_audit_reads_the_connections_notes_only(self):
        notes = [self._active("a1"), self._active("b2").model_copy(update={"note_id": "n2"})]
        assert [n.note_id for n in audit_candidates(notes, ref_tag="a1", now=NOW)] == ["n1"]

    def test_snapshots_follow_the_content_identity(self):
        identity = ContentIdentity("hashfam", "d1", 3)
        rec = DriveIndexRecord(user_id="usr-1", item_id="item-1", name="report.pdf", content_identity=identity)
        snap = snapshot_from_record(None, rec, path="report.pdf", now=NOW)
        assert snap.content_key == "hashfam:d1:3" and snap.content_key_family == "hashfam"
        assert not snapshot_stale(snap, rec)
        changed = refreshed_snapshot(snap, DriveChange(item_id="item-1", name="report.pdf"), path=None,
                                     now=NOW + timedelta(days=1))
        assert changed.content_key == "" and changed.content_key_family == ""

    def test_a_deleted_entry_merges_by_identity(self):
        note = self._active("a1")
        hit = note_hit(note, dormant=False, nonce="n0nce000", matched="")
        assert hit.item_key == "fake_a:drive-1:item-1"
