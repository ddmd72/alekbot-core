"""The owner's notes in the drive indexer's tick (DRIVE_FILE_NOTES_RFC §5.1, §5.5; plan part B): entry (a) merges a
created record's note in the same write (onboarding, a restore, a parent created by `_resolve_step`), re-activating an
archived note; entry (b) merges and clears from the note store's changes, paged on the watermark pair; a
NOTE_MERGE_VERSION bump re-merges every noted record and only those; one note count gates both; the vector repair and
the space pass; the closed access gate still clears a copy and its card; the lease fences note writes; the deadline sum
holds. The real DriveIndexingService on the in-memory index and note repositories. Synthetic items and notes only."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain import drive_note_merge as merge_mod
from src.domain.drive_card import card_hash, card_text
from src.domain.drive_extraction import PARSER, READER
from src.domain.drive_index import MIGRATION_GATE_KEY, KEYS_VERSION, Nudge
from src.domain.drive_index_params import (
    ENRICH_FILE_BUDGET_S, FILE_START_MARGIN_S, MIRROR_RESERVE_S, TICK_DEADLINE_S, TICK_START_BUDGET,
    TRANSCRIBE_EXTRACT_WALL_S, TRANSCRIBE_SHARE, TRANSCRIBE_UPLOAD_S,
)
from src.domain.drive_keys import note_keys
from src.domain.drive_note import (
    ONEDRIVE_PROVIDER_KEY, DriveNote, NoteAction, NoteBinding, NoteContent, NoteProvenance, NoteSnapshot, NoteState,
    note_after_write, next_version,
)
from src.domain.drive_note_merge import NOTE_EMBED_PER_TICK, NOTE_STEP_S, NOTE_WATERMARK_SKEW_S
from src.domain.user_drive import DriveConnection
from src.services import drive_indexing_service as svc_mod
from src.services.drive_indexing_service import DriveIndexingService

from .drive_content_fakes import FakeContentDrive, FakeExtractor, FakeTriage
from .drive_index_fakes import Clock, FakeEmbeddings, FakeQueue, FakeUsers

SPACE = "test-space/8"


class SpyNotes(InMemoryDriveNoteRepository):
    """The in-memory note store, counting the tick's reads and able to fail."""

    def __init__(self, clock: Any) -> None:
        super().__init__(clock)
        self.calls: List[Tuple[str, Any]] = []
        self.fail: Optional[Exception] = None
        self.hooks: Dict[str, Callable[[str], None]] = {}

    def _seen(self, name: str, arg: Any = None) -> None:
        self.calls.append((name, arg))
        if self.fail is not None:
            raise self.fail

    async def count(self, user_id: str) -> int:
        self._seen("count")
        return await super().count(user_id)

    async def find_by_match_keys(self, user_id: str, keys):  # type: ignore[override]
        self._seen("find_by_match_keys", tuple(keys))
        return await super().find_by_match_keys(user_id, keys)

    async def changed_since(self, user_id: str, after, limit: int, *, after_note_id: str = ""):  # type: ignore[override]
        self._seen("changed_since", (after, after_note_id))
        return await super().changed_since(user_id, after, limit, after_note_id=after_note_id)

    async def delete_stub(self, user_id: str, note_id: str) -> bool:
        self._seen("delete_stub", note_id)
        hook = self.hooks.get("delete_stub")
        if hook is not None:
            hook(note_id)
        return await super().delete_stub(user_id, note_id)

    def names(self) -> List[str]:
        return [n for n, _ in self.calls]


class World:
    def __init__(self, *, notes: bool = True) -> None:
        self.drive = FakeContentDrive()
        self.repo = InMemoryDriveIndexRepository()
        self.emb = FakeEmbeddings()
        self.queue = FakeQueue()
        self.users = FakeUsers()
        self.clock = Clock()
        self.notes = SpyNotes(self.clock)
        self.svc = DriveIndexingService(self.drive, self.repo, self.emb, self.queue, self.users,
                                        lambda lat, lon: None, SPACE,
                                        extractors={PARSER: FakeExtractor(PARSER), READER: FakeExtractor(READER)},
                                        triage=FakeTriage(), notes=self.notes if notes else None,
                                        clock=self.clock, monotonic=self.clock.monotonic)

    async def tick(self, nudge: Optional[Nudge] = None) -> Any:
        return await self.svc.tick("u", nudge)

    async def settle(self, max_ticks: int = 60) -> Any:
        for _ in range(max_ticks):
            before = len(self.queue.tasks)
            result = await self.tick()
            continued = [t for t in self.queue.tasks[before:] if ":cont:" in (t["key"] or "")]
            if not continued:
                return result
            self.clock.advance(max(1, max(t["delay"] for t in continued)))
        raise AssertionError("did not settle")

    def rec(self, item_id: str) -> Any:
        return self.repo.records.get(("u", item_id))

    def state(self) -> Any:
        return self.repo.states["u"]

    async def put_note(self, item_id: str, text: str = "the contractor's invoice", doc_type: str = "",
                       areas: Tuple[str, ...] = (), *, drive_id: str = "d1", note_id: str = "") -> DriveNote:
        """An owner `set` as the main service writes it (one version, the parent document)."""
        note_id = note_id or f"n-{item_id}"
        stored = await self.notes.get("u", note_id)
        base = stored or DriveNote(note_id=note_id, user_id="u", account_id="a")
        now = self.clock()
        version = next_version(base.current_version, NoteContent(text, doc_type, areas), NoteAction.SET,
                               NoteProvenance(turn_id="t"), now)
        binding = NoteBinding(provider_key=ONEDRIVE_PROVIDER_KEY, drive_id=drive_id, item_id=item_id, bound_at=now)
        written = note_after_write(base, version, NoteState.ACTIVE, NoteProvenance(turn_id="t"), now,
                                   vector=[0.1] * 8, embedding_space=SPACE, embedding_pending=False, binding=binding,
                                   snapshot=NoteSnapshot(name=item_id))
        await self.notes.write_version(written, version, expected_version=base.current_version)
        return (await self.notes.get("u", note_id))  # type: ignore[return-value]

    async def clear_note(self, note_id: str) -> None:
        note = await self.notes.get("u", note_id)
        assert note is not None
        now = self.clock()
        version = next_version(note.current_version, NoteContent(), NoteAction.CLEAR, NoteProvenance(turn_id="t"), now)
        written = note_after_write(note, version, NoteState.CLEARED, NoteProvenance(turn_id="t"), now, vector=None,
                                   embedding_space="", embedding_pending=False)
        await self.notes.write_version(written, version, expected_version=note.current_version)


@pytest.fixture
def w() -> World:
    return World()


def _copy(rec: Any) -> Tuple[str, str, str, Tuple[str, ...]]:
    return rec.owner_note_id, rec.owner_note_text, rec.owner_doc_type, tuple(rec.owner_life_areas)


# -- entry (a): created records (§5.1 a) -------------------------------------------------------------------------

async def test_onboarding_merges_every_note_in_the_creating_write(w):
    a = w.drive.add("a.pdf")
    b = w.drive.add("b.pdf")
    await w.put_note(a, "the contractor's invoice", "contract", ("home",))
    await w.settle()
    assert _copy(w.rec(a)) == (f"n-{a}", "the contractor's invoice", "contract", ("home",))
    assert w.rec(b).owner_note_id == ""
    assert "noted by the owner: the contractor's invoice; type contract; life areas home" in card_text(w.rec(a))
    assert set(note_keys("the contractor's invoice", "contract", ("home",))) <= set(w.rec(a).about_keys)
    assert not any(r.note_lookup_due for r in w.repo.records.values())   # cleared, found or not
    assert any("noted by the owner" in t for t in w.emb.calls)          # the card embedding carries the note


async def test_a_restore_reactivates_the_archived_note_and_merges_it(w):
    a = w.drive.add("a.pdf")
    await w.settle()
    note = await w.put_note(a)
    await w.notes.archive("u", note.note_id, archived_at=w.clock(), last_seen_at=w.clock())
    w.drive.delete(a)
    await w.settle()
    assert w.rec(a) is None
    w.drive.add("a.pdf", item_id=a)   # the recycle-bin restore: the same item id
    await w.settle()
    stored = await w.notes.get("u", note.note_id)
    assert stored.state == NoteState.ACTIVE.value and stored.binding.bound_by == "restore"
    assert stored.archived_at is None and w.rec(a).owner_note_id == note.note_id


async def test_a_cleared_archived_note_loses_its_stamp_and_stays_cleared(w):
    a = w.drive.add("a.pdf")
    await w.settle()
    note = await w.put_note(a)
    await w.clear_note(note.note_id)
    await w.notes.archive("u", note.note_id, archived_at=w.clock(), last_seen_at=w.clock())
    w.drive.delete(a)
    await w.settle()
    w.drive.add("a.pdf", item_id=a)
    await w.settle()
    stored = await w.notes.get("u", note.note_id)
    assert stored.state == NoteState.CLEARED.value and stored.archived_at is None
    assert w.rec(a).owner_note_id == ""


async def test_a_parent_created_by_the_resolve_step_gets_its_note(w):
    await w.settle()
    folder = w.drive.add("Bathroom", folder=True)
    w.drive.log.pop()   # delta never lists the folder itself: only its child's parent lookup creates it
    child = w.drive.add("plan.pdf", parent=folder)
    await w.put_note(folder, "the bathroom folder")
    await w.settle()
    assert w.rec(child) is not None and w.rec(folder) is not None
    assert w.rec(folder).owner_note_text == "the bathroom folder"
    assert not w.rec(folder).note_lookup_due


async def test_a_note_on_another_drive_is_never_merged(w):
    a = w.drive.add("a.pdf")
    await w.put_note(a, drive_id="d-old")
    await w.settle()
    assert w.rec(a).owner_note_id == ""


# -- entry (b): the notes step (§5.1 b) --------------------------------------------------------------------------

async def test_the_notes_step_merges_then_clears(w):
    a = w.drive.add("a.pdf")
    await w.settle()
    note = await w.put_note(a, "the contractor's invoice", "contract")
    await w.tick()
    assert _copy(w.rec(a))[:3] == (note.note_id, "the contractor's invoice", "contract")
    assert "noted by the owner" in card_text(w.rec(a)) and w.rec(a).due_at is None   # the card re-embedded
    await w.clear_note(note.note_id)
    w.clock.advance(1)
    await w.tick()
    assert w.rec(a).owner_note_id == "" and "noted by the owner" not in card_text(w.rec(a))
    assert not set(note_keys("the contractor's invoice")) & set(w.rec(a).about_keys)


async def test_the_watermark_is_the_last_note_and_a_new_pass_starts_a_skew_before(w):
    a = w.drive.add("a.pdf")
    await w.settle()
    note = await w.put_note(a)
    await w.tick()
    state = w.state()
    assert (state.notes_applied_at, state.notes_applied_id) == (note.updated_at, note.note_id)
    assert not state.notes_paging and state.notes_scanned_at == w.clock()
    w.clock.advance(5)
    w.notes.calls.clear()
    await w.tick()
    after = [arg for name, arg in w.notes.calls if name == "changed_since"][0]
    assert after == (state.notes_scanned_at - timedelta(seconds=NOTE_WATERMARK_SKEW_S), "")
    w.clock.advance(3600)
    await w.tick()
    w.notes.calls.clear()
    await w.tick()
    after = [arg for name, arg in w.notes.calls if name == "changed_since"][0]
    assert after == (note.updated_at, note.note_id)   # past the skew window: strictly after the pair


async def test_a_full_page_continues_on_the_pair_cursor(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "NOTE_PAGE", 2)
    items = [w.drive.add(f"f{i}.pdf") for i in range(5)]
    await w.settle()
    for item in items:
        await w.put_note(item, f"note {item}")
    w.notes.calls.clear()
    await w.settle()
    starts = [arg for name, arg in w.notes.calls if name == "changed_since"]
    assert starts[1][1] and starts[2][1]          # pages 2 and 3 start strictly after a pair
    assert all(w.rec(i).owner_note_text == f"note {i}" for i in items)
    assert not w.state().notes_paging


async def test_a_none_watermark_re_reads_every_note_in_pages(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "NOTE_PAGE", 2)
    items = [w.drive.add(f"f{i}.pdf") for i in range(5)]
    for item in items:
        await w.put_note(item, f"note {item}")
    await w.settle()
    w.repo.states["u"] = replace(w.state(), notes_applied_at=None, notes_applied_id="", notes_paging=False)
    for item in items:   # copies lost (as after a missed lookup): the re-read restores every one
        w.repo.records[("u", item)] = replace(w.rec(item), owner_note_id="", owner_note_text="",
                                              owner_note_version=0, owner_note_merge_version=0)
    w.notes.calls.clear()
    await w.settle()
    assert [arg[0] is None for name, arg in w.notes.calls if name == "changed_since"][0]
    assert all(w.rec(i).owner_note_text == f"note {i}" for i in items)


async def test_a_merge_version_bump_re_merges_the_noted_records_and_only_those(w, monkeypatch):
    noted = w.drive.add("a.pdf")
    plain = w.drive.add("b.pdf")
    await w.put_note(noted)
    await w.settle()
    before_plain = w.rec(plain)
    monkeypatch.setattr(svc_mod, "NOTE_MERGE_VERSION", 2)
    monkeypatch.setattr(merge_mod, "NOTE_MERGE_VERSION", 2)
    await w.settle()
    assert w.rec(noted).owner_note_merge_version == 2 and w.state().notes_merge_version == 2
    assert w.rec(plain) == before_plain


async def test_a_rolled_back_record_is_repaired_by_the_roll_forward_bump(w, monkeypatch):
    a = w.drive.add("a.pdf")
    await w.put_note(a, "the contractor's invoice")
    await w.settle()
    rec = w.rec(a)
    # the rolled-back code re-keyed and re-carded it without the note; the copy survived (merge=True writes)
    stale_keys = tuple(k for k in rec.about_keys if k not in set(note_keys("the contractor's invoice")))
    w.repo.records[("u", a)] = replace(rec, about_keys=stale_keys,
                                       card_hash=replace(rec, owner_note_id="", owner_note_text="").card_hash)
    monkeypatch.setattr(svc_mod, "NOTE_MERGE_VERSION", 2)
    monkeypatch.setattr(merge_mod, "NOTE_MERGE_VERSION", 2)
    await w.settle()
    assert set(note_keys("the contractor's invoice")) <= set(w.rec(a).about_keys)
    assert "noted by the owner" in card_text(w.rec(a))


async def test_the_count_gate_costs_one_read_for_a_user_without_notes(w):
    w.drive.add("a.pdf")
    await w.settle()
    w.drive.add("b.pdf")
    w.notes.calls.clear()
    await w.tick()
    assert w.notes.names() == ["count"]


async def test_a_nudge_after_a_write_merges_in_one_tick(w):
    a = w.drive.add("a.pdf")
    await w.settle()
    await w.put_note(a, "from the nudge")
    await w.tick(Nudge(mutation_id="m1", item_ids=(a,)))
    assert w.rec(a).owner_note_text == "from the nudge"


async def test_an_erase_stub_clears_the_copy_then_goes(w):
    a = w.drive.add("a.pdf")
    note = await w.put_note(a)
    await w.settle()
    await w.notes.erase("u", [note.note_id])
    w.clock.advance(1)
    await w.tick()
    assert w.rec(a).owner_note_id == "" and await w.notes.get("u", note.note_id) is None


async def test_a_cleared_note_never_clears_another_notes_copy(w):
    a = w.drive.add("a.pdf")
    await w.settle()
    await w.put_note(a, "kept", note_id="n-new")
    await w.tick()
    old = await w.put_note(a, "older lineage", note_id="n-old")
    await w.clear_note(old.note_id)
    w.clock.advance(1)
    await w.tick()
    assert (w.rec(a).owner_note_id, w.rec(a).owner_note_text) == ("n-new", "kept")


async def test_a_note_store_failure_skips_the_step_and_never_ends_the_tick(w):
    b = w.drive.add("b.pdf")
    await w.put_note(b)
    await w.settle()
    assert w.state().notes_applied_at is not None
    a = w.drive.add("a.pdf")
    await w.put_note(a)
    w.notes.fail = RuntimeError("index missing")
    result = await w.tick()
    assert result.status == "done" and w.rec(a) is not None and w.rec(a).owner_note_id == ""
    assert w.state().notes_applied_at is None          # the failed lookup asked for a re-read of every note
    w.notes.fail = None
    await w.settle()
    assert w.rec(a).owner_note_id == f"n-{a}" and w.rec(b).owner_note_id == f"n-{b}"


async def test_a_purge_leaves_the_note_store_untouched(w):
    a = w.drive.add("a.pdf")
    note = await w.put_note(a)
    await w.settle()
    w.users.enabled["u"] = False
    await w.settle()
    assert w.repo.records == {} and (await w.notes.get("u", note.note_id)).state == NoteState.ACTIVE.value
    w.users.enabled["u"] = True
    await w.settle()
    assert w.rec(a).owner_note_id == note.note_id   # onboarding merged it again (entry a)


async def test_a_keys_version_re_key_keeps_the_note_keys(w):
    a = w.drive.add("a.pdf")
    await w.put_note(a, "the contractor's invoice")
    await w.settle()
    w.repo.records[("u", a)] = replace(w.rec(a), keys_version=KEYS_VERSION - 1)
    await w.svc._commit("u", [w.rec(a)], set())   # any later write re-plans it (`planned`)
    assert set(note_keys("the contractor's invoice")) <= set(w.rec(a).about_keys)


async def test_without_the_store_no_notes_work_runs():
    w = World(notes=False)
    a = w.drive.add("a.pdf")
    await w.settle()
    assert w.rec(a).owner_note_id == "" and not w.rec(a).note_lookup_due


# -- the vector repair and the space pass (§5.1 b step 5) -----------------------------------------------------------

async def test_a_pending_vector_is_repaired_with_the_embedded_version(w):
    a = w.drive.add("a.pdf")
    await w.settle()
    note = await w.put_note(a)
    w.notes.notes[("u", note.note_id)] = note.model_copy(update={"note_vector": None, "embedding_pending": True,
                                                                 "embedding_space": ""})
    await w.tick()
    stored = await w.notes.get("u", note.note_id)
    assert not stored.embedding_pending and stored.note_vector and stored.embedding_space == SPACE


async def test_a_newer_version_keeps_its_own_vector(w, monkeypatch):
    a = w.drive.add("a.pdf")
    await w.settle()
    note = await w.put_note(a)
    w.notes.notes[("u", note.note_id)] = note.model_copy(update={"embedding_pending": True})
    real = w.emb.get_embedding
    raced: List[str] = []

    async def racing(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        if text.startswith("the contractor's invoice\n") and not raced:   # the note's own embedding text, once
            raced.append(text)
            await w.put_note(a, "a newer text")          # the owner writes while the repair embeds
        return await real(text, task_type)
    monkeypatch.setattr(w.emb, "get_embedding", racing)
    await w.tick()
    stored = await w.notes.get("u", note.note_id)
    assert raced and stored.current_version == 2 and stored.note_vector == [0.1] * 8   # nothing stored over it


async def test_the_space_pass_moves_every_note_and_stamps_the_space(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "NOTE_EMBED_PER_TICK", 2)
    items = [w.drive.add(f"f{i}.pdf") for i in range(5)]
    for item in items:
        await w.put_note(item, f"note {item}")
    await w.settle()
    assert w.state().notes_space == SPACE
    for key, note in list(w.notes.notes.items()):
        w.notes.notes[key] = note.model_copy(update={"embedding_space": "old-space/8"})
    w.repo.states["u"] = replace(w.state(), notes_space="old-space/8")
    for _ in range(6):
        await w.tick()
        w.clock.advance(1)
    assert all(n.embedding_space == SPACE for n in w.notes.notes.values())
    assert w.state().notes_space == SPACE and w.state().notes_space_at is None


async def test_the_space_pass_never_skips_a_note_whose_embedding_failed(w, monkeypatch):
    items = [w.drive.add(f"f{i}.pdf") for i in range(3)]
    for item in items:
        await w.put_note(item, f"note {item}")
    await w.settle()
    for key, note in list(w.notes.notes.items()):
        w.notes.notes[key] = note.model_copy(update={"embedding_space": "old-space/8"})
    w.repo.states["u"] = replace(w.state(), notes_space="old-space/8")
    real = w.emb.get_embedding

    async def failing(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        if f"note {items[1]}" in text:
            raise RuntimeError("quota")
        return await real(text, task_type)
    monkeypatch.setattr(w.emb, "get_embedding", failing)
    await w.tick()
    assert w.state().notes_space == "old-space/8"
    monkeypatch.setattr(w.emb, "get_embedding", real)
    for _ in range(3):
        w.clock.advance(1)
        await w.tick()
    assert all(n.embedding_space == SPACE for n in w.notes.notes.values()) and w.state().notes_space == SPACE


# -- the closed access gate (core review §5) ------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["clear", "erase"])
@pytest.mark.parametrize("gate", ["access_lost", "awaiting_migration"])
async def test_a_removed_note_leaves_the_record_and_card_behind_a_closed_gate(w, gate, command):
    a = w.drive.add("a.pdf")
    note = await w.put_note(a, "the contractor's invoice")
    await w.settle()
    assert w.rec(a).owner_note_id
    if gate == "access_lost":
        w.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")
    else:
        versions = {k: v for k, v in w.state().applied_versions.items() if k != MIGRATION_GATE_KEY}
        w.repo.states["u"] = replace(w.state(), applied_versions=versions)
    if command == "clear":
        await w.clear_note(note.note_id)
    else:
        await w.notes.erase("u", [note.note_id])
    w.clock.advance(1)
    w.emb.calls.clear()
    result = await w.tick()
    assert result.status == gate
    rec = w.rec(a)
    assert rec.owner_note_id == "" and not set(note_keys("the contractor's invoice")) & set(rec.about_keys)
    assert w.emb.calls and all("contractor" not in t for t in w.emb.calls)   # the card re-embedded without the note
    assert rec.has_card and rec.card_hash == card_hash(rec)
    assert w.repo.vectors[("u", a)] == await FakeEmbeddings().get_embedding(card_text(rec))   # the new card's vector
    if command == "erase":
        assert await w.notes.get("u", note.note_id) is None


async def test_behind_a_closed_gate_a_card_the_embedder_refuses_is_dropped(w, monkeypatch):
    a = w.drive.add("a.pdf")
    note = await w.put_note(a, "the contractor's invoice")
    await w.settle()
    w.drive.connection_state = DriveConnection(connected=False, drive_id="")
    await w.clear_note(note.note_id)

    async def down(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        raise RuntimeError("embedding outage")
    monkeypatch.setattr(w.emb, "get_embedding", down)
    assert (await w.tick()).status == "access_lost"
    rec = w.rec(a)
    assert rec.owner_note_id == "" and not rec.has_card and ("u", a) not in w.repo.vectors


# -- the lease and the deadline sum -------------------------------------------------------------------------------

async def test_the_lease_clock_stops_note_writes(w, monkeypatch):
    a = w.drive.add("a.pdf")
    await w.settle()
    note = await w.put_note(a)
    await w.notes.archive("u", note.note_id, archived_at=w.clock(), last_seen_at=w.clock())
    w.drive.delete(a)
    await w.settle()
    w.drive.add("a.pdf", item_id=a)   # the restore: a re-activation is due in the creating write
    armed: List[bool] = []
    real_find = w.notes.find_by_match_keys
    real_fence = svc_mod._fence

    async def find(user_id: str, keys: Any) -> Any:
        found = await real_find(user_id, keys)
        armed.append(True)             # the lease ends right after the lookup
        return found

    def fence() -> None:
        if armed:
            from src.domain.drive_index import LeaseLost
            raise LeaseLost("own lease ends")
        real_fence()
    monkeypatch.setattr(w.notes, "find_by_match_keys", find)
    monkeypatch.setattr(svc_mod, "_fence", fence)
    assert (await w.tick()).status == "lease_lost"
    assert armed and (await w.notes.get("u", note.note_id)).state == NoteState.ARCHIVED.value


def test_the_notes_step_keeps_the_deadline_sum():
    """The notes step starts only inside the start budget and starts no call NOTE_STEP_S after it began: its overrun
    past the start budget (NOTE_STEP_S + one call) is below an enrichment file's (ENRICH_FILE_BUDGET_S + one call),
    which the §7.1 sum already holds, and after an overrun nothing starts content work. The sum is unchanged."""
    total = TICK_START_BUDGET.total_seconds()
    extract_worst = TRANSCRIBE_EXTRACT_WALL_S + TRANSCRIBE_UPLOAD_S + FILE_START_MARGIN_S
    notes_overrun = NOTE_STEP_S + FILE_START_MARGIN_S
    enrich_overrun = ENRICH_FILE_BUDGET_S + FILE_START_MARGIN_S
    assert notes_overrun <= enrich_overrun
    assert (total + max(notes_overrun, enrich_overrun) + TRANSCRIBE_SHARE * total + extract_worst
            + MIRROR_RESERVE_S + FILE_START_MARGIN_S) < TICK_DEADLINE_S
    assert NOTE_EMBED_PER_TICK == 20


async def test_the_notes_step_does_not_start_past_the_start_budget(w):
    a = w.drive.add("a.pdf")
    await w.settle()
    await w.put_note(a)
    real = w.drive.get_changes

    async def slow(user_id: str, cursor: Any) -> Any:
        w.clock.advance(TICK_START_BUDGET.total_seconds() + 1)
        return await real(user_id, cursor)
    w.drive.get_changes = slow   # type: ignore[assignment]
    w.notes.calls.clear()
    await w.tick()
    assert "changed_since" not in w.notes.names() and w.rec(a).owner_note_id == ""
    w.drive.get_changes = real   # type: ignore[assignment]
    await w.tick()
    assert w.rec(a).owner_note_id == f"n-{a}"


# -- review fixes: the step's window, the kill switch, an incomplete round, the rollback detector --------------------

async def test_a_slow_embedding_ends_the_window_and_no_further_call_starts(w, monkeypatch):
    items = [w.drive.add(f"f{i}.pdf") for i in range(3)]
    await w.settle()
    for item in items:
        note = await w.put_note(item, f"note {item}")
        w.notes.notes[("u", note.note_id)] = note.model_copy(update={"embedding_pending": True})
    real = w.emb.get_embedding
    note_calls: List[str] = []

    async def slow(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        if text.startswith("note "):
            note_calls.append(text)
            w.clock.advance(NOTE_STEP_S + 1)      # one embedding outlasts the step's window
        return await real(text, task_type)
    monkeypatch.setattr(w.emb, "get_embedding", slow)
    await w.tick()
    assert len(note_calls) == 1
    pending = [n for n in w.notes.notes.values() if n.embedding_pending]
    assert len(pending) == 2                        # the rest stays pending for the next tick


async def test_a_stub_past_the_window_holds_the_watermark_and_the_next_tick_finishes(w):
    items = [w.drive.add(f"f{i}.pdf") for i in range(3)]
    for item in items:
        await w.put_note(item, f"note {item}")
    await w.settle()
    await w.notes.erase("u", [f"n-{i}" for i in items])
    w.notes.hooks["delete_stub"] = lambda note_id: w.clock.advance(NOTE_STEP_S + 1)
    w.clock.advance(1)
    await w.tick()
    assert sum(1 for n, _ in w.notes.calls if n == "delete_stub") == 1
    assert len(w.notes.notes) == 2 and w.state().notes_paging          # held right before the first stub left
    assert all(w.rec(i).owner_note_id == "" for i in items)            # every copy cleared in the one commit
    w.notes.hooks.clear()
    await w.settle()
    assert w.notes.notes == {}


async def test_a_failed_stub_delete_is_retried(w):
    a = w.drive.add("a.pdf")
    await w.put_note(a)
    await w.settle()
    await w.notes.erase("u", [f"n-{a}"])

    def boom(note_id: str) -> None:
        raise RuntimeError("store outage")
    w.notes.hooks["delete_stub"] = boom
    w.clock.advance(3600)   # the stub is older than any skew window: only a held watermark brings it back
    await w.tick()
    assert (await w.notes.get("u", f"n-{a}")) is not None
    w.notes.hooks.clear()
    w.clock.advance(3600)
    await w.tick()
    assert (await w.notes.get("u", f"n-{a}")) is None


def _service(w: World, notes: Any) -> DriveIndexingService:
    return DriveIndexingService(w.drive, w.repo, w.emb, w.queue, w.users, lambda lat, lon: None, SPACE,
                                extractors={PARSER: FakeExtractor(PARSER), READER: FakeExtractor(READER)},
                                triage=FakeTriage(), notes=notes, clock=w.clock, monotonic=w.clock.monotonic)


async def _settle_with(w: World, svc: DriveIndexingService) -> None:
    for _ in range(30):
        before = len(w.queue.tasks)
        await svc.tick("u")
        if not [t for t in w.queue.tasks[before:] if ":cont:" in (t["key"] or "")]:
            return
        w.clock.advance(1)


async def test_the_kill_switch_off_and_on_merges_the_records_created_meanwhile(w):
    a = w.drive.add("a.pdf")
    await w.put_note(a, "before")
    await w.settle()
    off = _service(w, None)                        # DRIVE_NOTES_ENABLED off: no store
    b = w.drive.add("b.pdf")
    await w.put_note(b, "made while off")
    await _settle_with(w, off)
    assert w.rec(b) is not None and w.rec(b).owner_note_id == ""
    assert w.state().notes_merge_version == 0      # saved by the tick with no store
    await w.settle()                               # the switch on again
    assert w.rec(b).owner_note_text == "made while off" and w.rec(a).owner_note_text == "before"


async def test_the_kill_switch_behind_a_closed_gate_saves_the_zero_version(w):
    a = w.drive.add("a.pdf")
    await w.put_note(a)
    await w.settle()
    w.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")
    assert (await _service(w, None).tick("u")).status == "access_lost"
    assert w.state().notes_merge_version == 0


async def test_an_erase_reaches_the_card_while_observe_uses_every_start_budget(w, monkeypatch):
    """Re-review item 4: an epoch re-enumeration's observe uses the whole start budget on every tick, so the notes step
    after it never runs; with the round left incomplete it runs first, and the existing card is embedded at once."""
    from src.domain.drive_index import DriveChanges
    a = w.drive.add("a.pdf")
    note = await w.put_note(a, "the contractor's invoice")
    await w.settle()
    assert w.rec(a).has_card and "contractor" in card_text(w.rec(a))
    real = w.drive.get_changes

    async def endless(user_id: str, cursor: Any) -> DriveChanges:
        page = await real(user_id, cursor)
        w.clock.advance(TICK_START_BUDGET.total_seconds() + 1)   # the page uses the start budget
        return replace(page, complete=False)                    # and the round never ends
    monkeypatch.setattr(w.drive, "get_changes", endless)
    await w.tick()
    assert not w.state().round_complete
    await w.notes.erase("u", [note.note_id])
    w.clock.advance(1)
    await w.tick()
    rec = w.rec(a)
    assert rec.owner_note_id == "" and not set(note_keys("the contractor's invoice")) & set(rec.about_keys)
    assert rec.card_hash == card_hash(rec)
    assert w.repo.vectors[("u", a)] == await FakeEmbeddings().get_embedding(card_text(rec))   # the new card's vector


async def test_the_notes_first_step_never_embeds_a_first_card(w, monkeypatch):
    """Only a stored vector can hold an old note: a record still waiting for its first card gets it in class order."""
    from src.domain.drive_index import DriveChanges
    a = w.drive.add("a.pdf")
    await w.settle()
    w.repo.records[("u", a)] = replace(w.rec(a), has_card=False, card_hash=None)
    w.repo.vectors.pop(("u", a), None)
    real = w.drive.get_changes

    async def endless(user_id: str, cursor: Any) -> DriveChanges:
        page = await real(user_id, cursor)
        w.clock.advance(TICK_START_BUDGET.total_seconds() + 1)
        return replace(page, complete=False)
    monkeypatch.setattr(w.drive, "get_changes", endless)
    await w.tick()
    await w.put_note(a, "the contractor's invoice")
    w.emb.calls.clear()
    await w.tick()
    rec = w.rec(a)
    assert rec.owner_note_text == "the contractor's invoice" and not rec.has_card and ("u", a) not in w.repo.vectors
    assert not any("noted by the owner" in t for t in w.emb.calls)


async def test_the_notes_step_runs_once_per_tick_on_either_path(w, monkeypatch):
    a = w.drive.add("a.pdf")
    await w.put_note(a)
    await w.settle()
    calls: List[str] = []
    real = w.svc._notes_step

    async def counted(*args: Any, **kw: Any) -> Any:
        calls.append("step")
        return await real(*args, **kw)
    monkeypatch.setattr(w.svc, "_notes_step", counted)
    await w.tick()
    w.repo.states["u"] = replace(w.state(), round_complete=False)
    await w.tick()
    assert calls == ["step", "step"]


async def test_the_kill_switch_zero_survives_an_error_save(w):
    a = w.drive.add("a.pdf")
    await w.put_note(a)
    await w.settle()
    assert w.state().notes_merge_version == 1
    w.drive.raise_next = RuntimeError("provider outage")      # the switch-off tick ends on the error path
    result = await _service(w, None).tick("u")
    assert result.status == "error" and w.state().notes_merge_version == 0


async def test_a_gated_tick_with_no_note_work_writes_no_state(w, monkeypatch):
    a = w.drive.add("a.pdf")
    await w.put_note(a)
    await w.settle()
    w.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")
    await w.tick()                                           # the loss is recorded (one save)
    saves: List[Any] = []
    real = w.repo.save_sync_state

    async def spy(state: Any, owner: Any = None) -> None:
        saves.append(state)
        await real(state, owner)
    monkeypatch.setattr(w.repo, "save_sync_state", spy)
    w.clock.advance(600)
    assert (await w.tick()).status == "access_lost"
    assert saves == []


async def test_a_stub_failing_for_a_day_is_passed(w):
    a = w.drive.add("a.pdf")
    b = w.drive.add("b.pdf")
    await w.put_note(a)
    await w.settle()
    await w.notes.erase("u", [f"n-{a}"])

    def boom(note_id: str) -> None:
        raise RuntimeError("store outage")
    w.notes.hooks["delete_stub"] = boom
    w.clock.advance(25 * 3600)                               # the stub is older than NOTE_STUB_HOLD_MAX
    await w.put_note(b, "after the stub")
    await w.tick()
    state = w.state()
    assert state.notes_applied_id == f"n-{b}" and not state.notes_paging   # the watermark passed the stub
    assert w.rec(b).owner_note_text == "after the stub"
    assert (await w.notes.get("u", f"n-{a}")) is not None   # left, contentless


async def test_a_tick_by_code_without_notes_forces_a_full_re_read(w):
    a = w.drive.add("a.pdf")
    await w.put_note(a, "kept")
    await w.settle()
    w.clock.advance(60)
    # the rolled-back revision ticks: it creates a record without a lookup, advances `last_success_at` and leaves the
    # notes fields as they were (it does not know them; its merge-save keeps them)
    b = w.drive.add("b.pdf")
    await w.put_note(b, "made during the rollback")
    before = w.state()
    await _settle_with(w, _service(w, None))
    w.repo.states["u"] = replace(w.state(), notes_merge_version=before.notes_merge_version,
                                 notes_tick_at=before.notes_tick_at)
    assert w.state().last_success_at > w.state().notes_tick_at
    w.notes.calls.clear()
    w.clock.advance(60)
    await w.settle()
    starts = [arg for name, arg in w.notes.calls if name == "changed_since"]
    assert starts[0] == (None, "")                 # the whole store read again
    assert w.rec(b).owner_note_text == "made during the rollback"


async def test_a_normal_tick_sequence_never_re_reads(w):
    a = w.drive.add("a.pdf")
    await w.put_note(a)
    await w.settle()
    w.notes.calls.clear()
    for _ in range(3):
        w.clock.advance(600)
        await w.tick()
    starts = [arg for name, arg in w.notes.calls if name == "changed_since"]
    assert starts and all(after is not None for after, _ in starts)
    assert w.state().notes_tick_at >= w.state().last_success_at


async def test_the_pass_end_is_read_before_the_notes_query(w, monkeypatch):
    a = w.drive.add("a.pdf")
    await w.settle()
    await w.put_note(a)
    real = w.notes.changed_since
    asked: List[Any] = []

    async def slow(user_id: str, after: Any, limit: int, *, after_note_id: str = "") -> Any:
        asked.append(w.clock())
        w.clock.advance(30)
        return await real(user_id, after, limit, after_note_id=after_note_id)
    monkeypatch.setattr(w.notes, "changed_since", slow)
    await w.tick()
    assert w.state().notes_scanned_at == asked[0]


async def test_an_archived_note_whose_record_is_live_is_reactivated_by_the_notes_step(w):
    # final review L-I1 (arbiter ruling; was "…_is_cleared_and_never_in_a_default_search"): §7.2 — an archived note
    # bound to this drive whose record exists has its file back, so the notes step re-activates it and merges it, the
    # same as entry (a) would have; the default search then finds it as the owner's note (it is active, not archived)
    from src.services.drive_search_service import DriveSearchService
    from src.domain.drive_search import DriveSearchRequest
    a = w.drive.add("a.pdf")
    note = await w.put_note(a, "zanzibar renovation")
    await w.settle()
    await w.notes.archive("u", note.note_id, archived_at=w.clock(), last_seen_at=w.clock())
    w.clock.advance(1)
    await w.tick()
    stored = await w.notes.get("u", note.note_id)
    assert stored.state == "active" and stored.archived_at is None
    assert w.rec(a) is not None and w.rec(a).owner_note_id == note.note_id
    search = DriveSearchService(w.repo, w.emb, w.users, w.drive, embedding_space=SPACE, clock=w.clock)
    result = await search.search("u", DriveSearchRequest(query="zanzibar"))
    assert [c.owner_note for c in result.cards if c.tier == "about"]


async def test_a_page_note_written_in_the_old_space_is_moved(w):
    a = w.drive.add("a.pdf")
    await w.put_note(a)
    await w.settle()
    assert w.state().notes_space == SPACE
    note = await w.put_note(a, "written by an old main service")
    w.notes.notes[("u", note.note_id)] = note.model_copy(update={"embedding_space": "old-space/8"})
    w.clock.advance(1)
    await w.tick()
    assert (await w.notes.get("u", note.note_id)).embedding_space == SPACE
