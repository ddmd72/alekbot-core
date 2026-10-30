"""The pure rules of an owner note's lifecycle in the indexer (DRIVE_FILE_NOTES_RFC §3.4, §7.1, §7.4, §7.5): which
deleted records report a check, how checks fold, which notes the audit reads, and the snapshot an observation leaves."""
from datetime import datetime, timedelta, timezone

from src.domain.drive_index import DriveChange, DriveIndexRecord
from src.domain.drive_mirror import GONE, UNSTORED
from src.domain.drive_note import (
    CONTENT_KEY_FAMILY, ONEDRIVE_PROVIDER_KEY, DriveNote, NoteBinding, NoteSnapshot, NoteState, binding_key,
    erase_stub, ref_key,
)
from src.domain.drive_note_check import WHY_GONE, WHY_UNSTORED, NoteCheck
from src.domain.drive_index import DriveSyncState
from src.domain.drive_note_lifecycle import (
    NOTE_AUDIT_CALL_SHARE, NOTE_CHECKS_STORED_MAX, NOTE_ROUND_END_CALL_SHARE, audit_candidates, call_share, audit_cursor, audit_due, audit_key, chain_path,
    checks_of, fold_checks, moved_snapshot, record_moved, refreshed_snapshot, snapshot_from_record, snapshot_stale,
    without_checks,
)
from src.domain.drive_note_merge import NOTE_AUDIT_EVERY, NOTE_AUDIT_SKIP_DAYS, NOTE_CHECKS_MAX

T = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
P = ONEDRIVE_PROVIDER_KEY


def _check(note_id, item_id="i", why=WHY_GONE):
    return NoteCheck(note_id=note_id, item_id=item_id, why=why, last_seen_at=T)


def _note(note_id, *, item="a", drive="d1", provider=P, state=NoteState.ACTIVE, seen=None):
    binding = NoteBinding(provider_key=provider, drive_id=drive, item_id=item)
    return DriveNote(note_id=note_id, user_id="u", state=state, current_version=1, text="x", binding=binding,
                     snapshot=NoteSnapshot(name=f"{item}.pdf", path=f"Docs/{item}.pdf", snapshot_at=seen),
                     match_keys=[binding_key(provider, drive, item), ref_key(item)])


def test_the_why_words_are_the_transcript_mirrors():
    assert (WHY_GONE, WHY_UNSTORED) == (GONE, UNSTORED)


def test_the_parameters():
    """C's two caps; the period constants are part B's (drive_note_merge, RFC §15), imported."""
    assert NOTE_CHECKS_STORED_MAX == 2000 and NOTE_AUDIT_CALL_SHARE == 0.65
    assert int(20 * NOTE_AUDIT_CALL_SHARE) == 13          # of the default ITEM_CALLS_PER_TICK: 7 stay for the tick
    assert NOTE_ROUND_END_CALL_SHARE == 0.65


def test_call_share():
    """D-C18, D-C20: the share of the calls left, at least one while any is left, none when none is."""
    assert call_share(20, 0.65) == 13 and call_share(7, 0.65) == 4 and call_share(1, 0.65) == 1
    assert call_share(0, 0.65) == 0 and call_share(-1, 0.65) == 0
    assert NOTE_CHECKS_MAX == 500 and NOTE_AUDIT_EVERY == timedelta(hours=24) and NOTE_AUDIT_SKIP_DAYS == 7


class TestChecksOf:

    def test_only_records_with_a_note_report_one(self):
        recs = [DriveIndexRecord(user_id="u", connection_id="u", item_id="a", owner_note_id="n1"),
                DriveIndexRecord(user_id="u", connection_id="u", item_id="b")]
        assert checks_of(recs, WHY_UNSTORED, T) == (NoteCheck("n1", "a", WHY_UNSTORED, T),)

    def test_no_record_no_check(self):
        assert checks_of([], WHY_GONE, T) == ()


class TestFold:

    def test_one_check_per_note_and_the_later_why_wins_in_place(self):
        stored = (_check("n1", why=WHY_UNSTORED), _check("n2"))
        folded, dropped = fold_checks(stored, [_check("n3"), _check("n1", why=WHY_GONE)])
        assert [c.note_id for c in folded] == ["n1", "n2", "n3"] and dropped == 0
        assert folded[0].why == WHY_GONE

    def test_past_the_cap_the_newest_are_dropped(self):
        folded, dropped = fold_checks([_check(f"s{i}") for i in range(3)], [_check("x"), _check("y")], cap=4)
        assert [c.note_id for c in folded] == ["s0", "s1", "s2", "x"] and dropped == 1

    def test_without_checks(self):
        assert without_checks((_check("n1"), _check("n2")), {"n1"}) == (_check("n2"),)


class TestAudit:

    def test_due_once_a_day(self):
        assert audit_due(None, T) is True
        assert audit_due(T - NOTE_AUDIT_EVERY, T) is True
        assert audit_due(T - NOTE_AUDIT_EVERY + timedelta(seconds=1), T) is False

    def test_candidates_are_active_on_the_drive_and_unseen_for_seven_days(self):
        old = T - timedelta(days=NOTE_AUDIT_SKIP_DAYS, seconds=1)
        fresh = T - timedelta(days=NOTE_AUDIT_SKIP_DAYS) + timedelta(seconds=1)
        notes = [
            _note("never", item="a"),
            _note("old", item="b", seen=old),
            _note("fresh", item="c", seen=fresh),
            _note("dormant", item="d", drive="d2", seen=old),
            _note("other-provider", item="e", provider="gdrive", seen=old),
            _note("archived", item="f", state=NoteState.ARCHIVED, seen=old),
            _note("cleared", item="g", state=NoteState.CLEARED, seen=old),
            erase_stub(_note("stub", item="h", seen=old)),
        ]
        got = audit_candidates(notes, provider_key=P, drive_id="d1", now=T)
        assert [n.note_id for n in got] == ["never", "old"]     # never seen first, then the longest unseen

    def test_a_resumed_pass_starts_after_the_cursor(self):
        old = T - timedelta(days=NOTE_AUDIT_SKIP_DAYS + 3)
        notes = [_note("a", item="a"), _note("b", item="b", seen=old), _note("c", item="c", seen=old),
                 _note("d", item="d", seen=old + timedelta(days=1))]
        after = audit_key(notes[1])
        got = audit_candidates(notes, provider_key=P, drive_id="d1", now=T, after=after)
        assert [n.note_id for n in got] == ["c", "d"]
        assert audit_key(notes[0]) < audit_key(notes[1]) < audit_key(notes[2])

    def test_the_cursor_of_the_state(self):
        assert audit_cursor(DriveSyncState(user_id="u", connection_id="u")) is None
        never = audit_cursor(DriveSyncState(user_id="u", connection_id="u", notes_audit_after_id="n1"))
        assert never is not None and never[1] == "n1" and never == audit_key(_note("n1"))
        dated = audit_cursor(DriveSyncState(user_id="u", connection_id="u", notes_audit_after_at=T, notes_audit_after_id="n2"))
        assert dated == (T, "n2")


class TestSnapshots:

    def test_chain_path(self):
        assert chain_path("a.pdf", ["Sub", "Docs"]) == "Docs/Sub/a.pdf"
        assert chain_path("a.pdf", []) == "a.pdf"

    def test_refreshed_from_the_observation_kind_and_date_kept(self):
        old = NoteSnapshot(name="old.pdf", path="Docs/old.pdf", parent_item_id="p0", kind="pdf",
                           effective_date=T - timedelta(days=9), content_key="q0:1", snapshot_at=T - timedelta(days=30))
        change = DriveChange(item_id="a", parent_id="p1", name="new.pdf", size_bytes=7, mime_type="application/pdf",
                             fs_modified_at=T - timedelta(days=1), facets={"hashes": {"quickXorHash": "qx"}})
        got = refreshed_snapshot(old, change, path="Repo/new.pdf", now=T)
        assert (got.name, got.path, got.parent_item_id, got.size_bytes) == ("new.pdf", "Repo/new.pdf", "p1", 7)
        assert (got.content_key, got.content_key_family, got.snapshot_at) == ("qx:7", CONTENT_KEY_FAMILY, T)
        assert (got.kind, got.effective_date) == ("pdf", T - timedelta(days=9))
        assert old.path == "Docs/old.pdf"                       # never mutated

    def test_refreshed_keeps_the_path_when_the_chain_is_unknown(self):
        old = NoteSnapshot(name="a.pdf", path="Docs/a.pdf")
        got = refreshed_snapshot(old, DriveChange(item_id="a", parent_id="p", name="a.pdf"), path=None, now=T)
        assert got.path == "Docs/a.pdf" and got.content_key == "" and got.content_key_family == ""

    def test_moved_snapshot_only_when_the_path_changed(self):
        old = NoteSnapshot(name="a.pdf", path="Docs/a.pdf", snapshot_at=T - timedelta(days=2))
        assert moved_snapshot(old, None) is None and moved_snapshot(old, "Docs/a.pdf") is None
        moved = moved_snapshot(old, "Archive/a.pdf")
        assert moved is not None and moved.path == "Archive/a.pdf" and moved.snapshot_at == old.snapshot_at
        assert moved_snapshot(None, "x") is None


class TestRecordSnapshots:

    def _rec(self, **kw):
        base = dict(user_id="u", item_id="a", name="a.pdf", parent_id="p", content_hash="q:1", kind="pdf",
                    mime_type="application/pdf", size_bytes=1)
        base.update(kw)
        return DriveIndexRecord(**base)

    def test_record_moved(self):
        rec = self._rec()
        assert not record_moved(rec, self._rec(size_bytes=9))
        assert record_moved(rec, self._rec(name="b.pdf")) and record_moved(rec, self._rec(parent_id="q"))
        assert record_moved(rec, self._rec(content_hash="q:2"))

    def test_snapshot_stale(self):
        rec = self._rec()
        assert snapshot_stale(None, rec)
        assert not snapshot_stale(NoteSnapshot(name="a.pdf", parent_item_id="p", content_key="q:1"), rec)
        assert snapshot_stale(NoteSnapshot(name="a.pdf", parent_item_id="p", content_key=""), rec)

    def test_snapshot_from_record(self):
        rec = self._rec(effective_date=T - timedelta(days=3))
        got = snapshot_from_record(NoteSnapshot(name="old.pdf", path="x/old.pdf"), rec, path="Docs/a.pdf", now=T)
        assert (got.name, got.path, got.parent_item_id, got.kind, got.content_key, got.snapshot_at) == (
            "a.pdf", "Docs/a.pdf", "p", "pdf", "q:1", T)
        assert got.content_key_family == CONTENT_KEY_FAMILY and got.effective_date == T - timedelta(days=3)
        assert snapshot_from_record(NoteSnapshot(path="keep"), rec, path=None, now=T).path == "keep"
