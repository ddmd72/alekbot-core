"""The note side of the deleted-files search, pure (DRIVE_FILE_NOTES_RFC §3.1 keys, §6.2 the note line, §6.4 the
entry) and the shared section's merge of one item's hits (§0.3)."""
from datetime import datetime, timezone

from src.domain.drive_card import owner_note_line
from src.domain.drive_deleted_search import DeletedHit, merge_hits, render_deleted
from src.domain.drive_keys import name_keys_v2, query_concepts
from src.domain.drive_note import DriveNote, NoteBinding, NoteSnapshot, NoteState
from src.domain.drive_note_merge import NOTE_STORE_KEYS_VERSION
from src.domain.drive_note_render import matched_line, note_hit, note_matches, search_keys

T = datetime(2026, 10, 1, tzinfo=timezone.utc)


def store_keys(content, name):
    """A stand-in for part B's `note_store_keys`: `e:` of each word and classifier, then the name keys v2."""
    words = tuple(f"e:{w.lower()}" for w in (*content.text.split(), content.doc_type, *content.life_areas) if w)
    return list(dict.fromkeys((*words, *name_keys_v2(name)))), NOTE_STORE_KEYS_VERSION


def _concepts(q):
    return query_concepts(q).concepts


def _note(**kw):
    base = dict(note_id="n1", user_id="u", state=NoteState.ARCHIVED, current_version=1, text="contractor bathroom",
                binding=NoteBinding(identity="fake:d1:i1", item_id="i1", ref_tag="a1"),
                snapshot=NoteSnapshot(name="invoice.pdf", path="Home/invoice.pdf"), archived_at=T)
    base.update(kw)
    return DriveNote(**base)


class TestKeys:

    def test_search_keys_stored_or_recomputed_with_the_stores_function(self):
        stored = _note(keys=["e:x"], keys_version=NOTE_STORE_KEYS_VERSION)
        # final review L-M2: the stored keys plus the current snapshot name's keys (a renamed file's last known name)
        assert search_keys(stored, store_keys) == {"e:x", *name_keys_v2(stored.snapshot.name)}
        assert "e:bathroom" in search_keys(_note(keys=["e:x"], keys_version="old"), store_keys)
        assert "e:bathroom" in search_keys(_note(), store_keys)          # S1 wrote no keys

    def test_the_default_is_part_bs_function(self):
        """With no injection the keys come from `drive_note_merge.note_store_keys` (the name keys at least)."""
        assert set(name_keys_v2("invoice.pdf")) <= search_keys(_note())


class TestMatch:

    def test_strict_and(self):
        keys = search_keys(_note(), store_keys)
        assert note_matches(_concepts("bathroom invoice"), keys)
        assert not note_matches(_concepts("bathroom lisbon"), keys)
        assert note_matches((), keys)

    def test_matched_line_names_where(self):
        keys = search_keys(_note(), store_keys)
        names = set(name_keys_v2("invoice.pdf"))
        assert matched_line(_concepts("bathroom invoice"), keys - names, names) == \
            'matched: owner\'s note "bathroom", name "invoice"'
        assert matched_line((), keys, names) == ""


class TestHit:

    def test_archived_entry(self):
        hit = note_hit(_note(doc_type="contract"), dormant=False, nonce="abcdef12", matched="matched: x",
                       day="2026-10-01")
        assert (hit.name, hit.path, hit.day, hit.ref, hit.item, hit.item_key) == (
            "invoice.pdf", "Home/invoice.pdf", "2026-10-01", "drive_note:n1", None, "fake:d1:i1")   # the item identity
        assert hit.note_line == "noted by the owner: «contractor bathroom» (type contract)\nmatched: x"
        assert hit.note_line.split("\n")[0] == owner_note_line("contractor bathroom", "contract", ())   # part B's renderer
        out = render_deleted([hit], connected=True)
        assert "invoice.pdf | Home/invoice.pdf | gone from the drive (deleted or moved out of the bot's folder), " \
               "2026-10-01" in out
        assert "drive_note:n1 — the file is gone: this ref works only with the note tools" in out

    def test_dormant_entry(self):
        hit = note_hit(_note(state=NoteState.ACTIVE, archived_at=None), dormant=True, nonce="abcdef12", matched="")
        out = render_deleted([hit], connected=True)
        assert "| on a drive that is not connected" in out and "on a drive that is not connected: this ref" in out


class TestMerge:

    def test_one_entry_per_item(self):
        transcript = DeletedHit(name="call.m4a", path="Calls/call.m4a", refs=("archived transcript: drive:m1",),
                                nonce="aaaa1111", ref="drive:m1", item_key="d1:i1")
        note = DeletedHit(name="call.m4a", path="Calls/call.m4a", refs=("drive_note:n1 — …",),
                          note_line="noted by the owner: «x»", nonce="bbbb2222", ref="drive_note:n1", item_key="d1:i1")
        other = DeletedHit(name="b.pdf", path="b.pdf", nonce="cccc3333", ref="drive_note:n2", item_key="d1:i2")
        keyless = DeletedHit(name="c.pdf", path="c.pdf", nonce="dddd4444", ref="drive_note:n3")
        merged = merge_hits([transcript, other, note, keyless, keyless])
        assert [h.ref for h in merged] == ["drive:m1", "drive_note:n2", "drive_note:n3", "drive_note:n3"]
        assert merged[0].refs == ("archived transcript: drive:m1", "drive_note:n1 — …")
        assert merged[0].note_line == "noted by the owner: «x»"
