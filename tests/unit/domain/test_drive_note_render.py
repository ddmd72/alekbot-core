"""The note side of the deleted-files search, pure (DRIVE_FILE_NOTES_RFC §3.1 keys, §6.2 the note line, §6.4 the
entry) and the shared section's merge of one item's hits (§0.3)."""
from datetime import datetime, timezone

from src.domain.drive_deleted_search import DeletedHit, merge_hits, render_deleted
from src.domain.drive_index import KEYS_VERSION
from src.domain.drive_keys import name_keys_v2, query_concepts
from src.domain.drive_note import (
    NOTE_KEYS_VERSION, ONEDRIVE_PROVIDER_KEY, DriveNote, NoteBinding, NoteContent, NoteSnapshot, NoteState,
)
from src.domain.drive_note_render import (
    NOTE_STORE_KEYS_VERSION, matched_line, note_hit, note_matches, note_store_keys, owner_note_line, search_keys,
)

T = datetime(2026, 10, 1, tzinfo=timezone.utc)


def keyer(text, doc_type, life_areas):
    return tuple(f"e:{w.lower()}" for w in (*text.split(), doc_type, *life_areas) if w)


def _concepts(q):
    return query_concepts(q).concepts


def _note(**kw):
    base = dict(note_id="n1", user_id="u", state=NoteState.ARCHIVED, current_version=1, text="Petya kitchen",
                binding=NoteBinding(provider_key=ONEDRIVE_PROVIDER_KEY, drive_id="d1", item_id="i1"),
                snapshot=NoteSnapshot(name="estimate.pdf", path="Home/estimate.pdf"), archived_at=T)
    base.update(kw)
    return DriveNote(**base)


class TestNoteLine:

    def test_text_and_classifiers(self):
        assert owner_note_line("Petya's estimate", "contract", ["home", "legal_admin"]) == \
            "noted by the owner: «Petya's estimate» (type contract; life areas home, legal admin)"

    def test_classifier_only_has_no_empty_quote(self):
        assert owner_note_line("", "contract") == "noted by the owner: type contract"

    def test_text_only_and_empty(self):
        assert owner_note_line("x") == "noted by the owner: «x»" and owner_note_line("") == ""


class TestKeys:

    def test_the_layout_version(self):
        assert NOTE_STORE_KEYS_VERSION == f"{KEYS_VERSION}.{NOTE_KEYS_VERSION}"

    def test_note_keys_then_name_keys(self):
        keys = note_store_keys(NoteContent("Petya kitchen", "contract", ()), "estimate.pdf", keyer)
        assert keys[:3] == ("e:petya", "e:kitchen", "e:contract")
        assert set(name_keys_v2("estimate.pdf")) <= set(keys) and len(keys) == len(set(keys))

    def test_an_empty_triple_has_only_name_keys(self):
        assert note_store_keys(NoteContent(), "a.pdf", keyer) == name_keys_v2("a.pdf")

    def test_search_keys_stored_or_recomputed(self):
        stored = _note(keys=["e:x"], keys_version=NOTE_STORE_KEYS_VERSION)
        assert search_keys(stored, keyer) == {"e:x"}
        assert "e:kitchen" in search_keys(_note(keys=["e:x"], keys_version="old"), keyer)
        assert "e:kitchen" in search_keys(_note(), keyer)          # S1 wrote no keys


class TestMatch:

    def test_strict_and(self):
        keys = search_keys(_note(), keyer)
        assert note_matches(_concepts("kitchen estimate"), keys)
        assert not note_matches(_concepts("kitchen lisbon"), keys)
        assert note_matches((), keys)

    def test_matched_line_names_where(self):
        keys = search_keys(_note(), keyer)
        names = set(name_keys_v2("estimate.pdf"))
        assert matched_line(_concepts("kitchen estimate"), keys - names, names) == \
            'matched: your note "kitchen", name "estimate"'
        assert matched_line((), keys, names) == ""


class TestHit:

    def test_archived_entry(self):
        hit = note_hit(_note(doc_type="contract"), dormant=False, nonce="abcdef12", matched="matched: x",
                       day="2026-10-01")
        assert (hit.name, hit.path, hit.day, hit.ref, hit.item, hit.item_key) == (
            "estimate.pdf", "Home/estimate.pdf", "2026-10-01", "drive_note:n1", None, "d1:i1")
        assert hit.note_line == "noted by the owner: «Petya kitchen» (type contract)\nmatched: x"
        out = render_deleted([hit], connected=True)
        assert "estimate.pdf | Home/estimate.pdf | gone from the drive (deleted or moved out of the bot's folder), " \
               "2026-10-01" in out
        assert "drive_note:n1 — the file is gone: this ref works only for annotate_drive_file" in out

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
