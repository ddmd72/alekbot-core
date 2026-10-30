"""`DriveSearchService.excerpts` / `.excerpt_range`: the passages nearest a content question and one file's passages by
number (USER_DRIVE_INDEXING_RFC §28.3 A4-A8; plan S1.2). The real in-memory repository (brute-force cosine) and fixed
query and chunk vectors; synthetic records from the invented universe only."""
from __future__ import annotations

import itertools
import re
from collections import Counter
from dataclasses import replace
from typing import List
from unittest.mock import MagicMock

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_index import DocumentBrief, DriveChunk, DriveIndexRecord, RecordState, Scope
from src.domain.drive_index_params import CHUNK_FETCH, EXCERPTS_CHARS_MAX, FETCH_MAX, FOLDER_INMEM_CARDS
from src.domain.drive_keys import word_keys
from src.domain.drive_search import (
    STATUS_AMBIGUOUS_FOLDER, STATUS_DISABLED, STATUS_DISCONNECTED, STATUS_FOLDER_NOT_FOUND, STATUS_OK,
)
from src.domain.drive_content_identity import ContentIdentity
from src.domain.user_drive import DriveConnection
from src.services import drive_search_service as search_mod
from src.services.drive_search_service import DriveSearchService

from .drive_harness_connections import harness_connections
from .drive_search_v2_fixture import SPACE, V2World

QUESTION = "how does the review process work"


def toward(d: float) -> List[float]:
    """A vector at an angle from the question's (e0) that grows with `d`: smaller `d`, nearer."""
    return [1.0, d, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


class CountingRepo(InMemoryDriveIndexRepository):
    """Counts the reads a passages call makes; semantics are the in-memory adapter's."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: Counter = Counter()
        self.knn_limits: List[int] = []
        self.scoped_knn: List[tuple] = []   # (limit, item ids) per kNN over a set of files (RFC Δ246)
        self.record_reads: List[tuple] = []

    async def get_records(self, user_id, item_ids):
        self.record_reads.append(tuple(item_ids))
        return await super().get_records(user_id, item_ids)

    async def find_nearest_chunks(self, user_id, vector, limit, kind=None):
        self.knn_limits.append(limit)
        return await super().find_nearest_chunks(user_id, vector, limit, kind)

    async def find_nearest_chunks_in(self, user_id, vector, limit, item_ids):
        self.scoped_knn.append((limit, tuple(item_ids)))
        return await super().find_nearest_chunks_in(user_id, vector, limit, item_ids)

    async def get_chunks_with_vectors(self, user_id, item_id, generation, indexes, **kw):
        self.calls["vectors"] += 1
        return await super().get_chunks_with_vectors(user_id, item_id, generation, indexes, **kw)

    async def get_chunks(self, user_id, item_id, generation, indexes):
        self.calls["texts"] += 1
        return await super().get_chunks(user_id, item_id, generation, indexes)

    async def get_chunk_keys(self, user_id, ids):
        self.calls["keys"] += 1
        return await super().get_chunk_keys(user_id, ids)


class PassageWorld(V2World):
    """V2World's records on a counting repository; the question's vector is e0 and chunk vectors are set by hand."""

    def __init__(self, *, migrated: bool = True, space: str = SPACE) -> None:
        super().__init__(migrated=migrated)
        state = self.repo.states["u"]
        self.repo = CountingRepo()
        self.repo.states["u"] = state
        counter = itertools.count(1)
        self.connections = harness_connections(self.users, self.drive)
        self.service = DriveSearchService(self.repo, self.emb, self.drive, self.connections, embedding_space=space,
                                          clock=lambda: self.now, nonce=lambda: f"nonce{next(counter):04d}")
        self.emb.pinned[QUESTION] = toward(0.0)

    def doc(self, name: str, texts, distances, *, parent: str = "root", generation: int = 1,
            content_hash: str = "", **fields) -> str:
        item = self.file(name, parent=parent, kind=fields.pop("kind", "docx"), chunks=texts, generation=generation,
                         **fields)
        if content_hash:
            digest, size = content_hash.split(":")
            self.set(item, content_identity=ContentIdentity("fakehash", digest, int(size)))
        for index, d in enumerate(distances):
            chunk, _ = self.repo.chunks[("u", item, generation, index)]
            self.repo.chunks[("u", item, generation, index)] = (chunk, toward(d))
        return item

    def stale_chunk(self, item: str, generation: int, index: int, text: str, d: float) -> None:
        chunk = DriveChunk(user_id="u", connection_id="u", item_id=item, generation=generation, index=index, text=text, char_start=0,
                           char_end=len(text), kind="docx", word_keys=word_keys(text))
        self.repo.chunks[("u", item, generation, index)] = (chunk, toward(d))

    def note(self, item: str, text: str) -> None:
        self.set(item, owner_note_id=f"n-{item}", owner_note_version=1, owner_note_text=text,
                 owner_note_merge_version=1)

    async def find(self, question: str = QUESTION, *, shown=frozenset(), **kw):
        return await self.service.excerpts("u", ExcerptRequest(question=question, **kw), shown=shown)

    async def connection(self):
        """The harness connection as read now (its switch and state are derived live from the profile and drive)."""
        return await self.connections.get("u", "u")

    async def read(self, item: str, first: int, last: int):
        return await self.service.excerpt_range("u", ExcerptRangeRequest(ref(item), first, last))


def ref(item: str) -> str:
    """The file's ref (was `make_drive_ref`, which left `src.domain.user_drive`)."""
    return f"drive:{item}"


def texts(result) -> List[str]:
    return [c.text for f in result.files for c in f.chunks]


def file_of(result, item: str):
    return next(f for f in result.files if f.item_id == item)


@pytest.fixture
def w() -> PassageWorld:
    return PassageWorld()


# -- the whole drive ------------------------------------------------------------------------------------------------

async def test_the_whole_drive_ranks_by_the_question_and_fills_the_file(w):
    rd = w.folder("R&D")
    plan = w.doc("Tom's plan.docx", ["The first review is held by Lukas Weber.", "Then Acme signs off."],
                 [0.1, 0.3], parent=rd, capture="digital", truncated=True, content_version="v1",
                 indexed_version="v1")
    w.doc("menu.md", ["Lunch options near the office."], [5.0], kind="md")
    result = await w.find()
    assert result.status == STATUS_OK and w.repo.knn_limits == [CHUNK_FETCH]
    f = result.files[0]
    assert (f.item_id, f.ref, f.name, f.path, f.kind) == (plan, ref(plan), "Tom's plan.docx",
                                                          "R&D/Tom's plan.docx", "docx")
    assert [c.index for c in f.chunks] == [0, 1] and f.chunk_count == 2 and f.generation == 1
    assert f.capture == "digital" and f.truncated and not f.stale
    assert result.folder_path == ""


async def test_the_whole_drive_counts_no_files_without_text(w):
    """Confirmed choice: only file refs and a complete folder walk count the files with no indexed text."""
    w.doc("Tom's plan.docx", ["The review is held weekly."], [0.1])
    w.file("budget.ods", kind="ods")
    result = await w.find()
    assert result.no_text == 0 and result.no_text_paths == ()


async def test_an_older_generation_is_never_shown(w):
    plan = w.doc("Tom's plan.docx", ["Current text of the plan."], [0.5], generation=2)
    w.stale_chunk(plan, 1, 0, "Old text of the plan.", 0.0)   # nearer than anything, but not the readable generation
    result = await w.find()
    assert texts(result) == ["Current text of the plan."]
    assert all(c.generation == 2 for c in result.files[0].chunks)


async def test_a_file_under_an_opaque_ancestor_is_hidden(w):
    code = w.folder("Code")
    w.set(code, scope=Scope.OPAQUE_ROOT)
    w.doc("notes.md", ["Review steps of the build."], [0.0], parent=code, kind="md")
    plan = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.4])
    result = await w.find()
    assert [f.item_id for f in result.files] == [plan]


async def test_a_deleting_record_is_not_fenced_in(w):
    gone = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.0])
    w.set(gone, state=RecordState.DELETING)
    assert (await w.find()).files == ()


async def test_an_id_held_by_a_chunk_is_lifted_among_the_nearest(w):
    near = w.doc("menu.md", ["Lunch options near the office."], [0.1], kind="md")
    held = w.doc("Tom's plan.docx", ["Acme order 4711 for the review."], [0.2])
    w.emb.pinned["order 4711"] = toward(0.0)
    result = await w.find("order 4711")
    assert [f.item_id for f in result.files] == [held, near]
    assert w.repo.calls["keys"] == 1


# -- file refs --------------------------------------------------------------------------------------------------------

async def test_file_refs_rank_only_those_files_in_memory(w):
    plan = w.doc("Tom's plan.docx", ["The review is held weekly.", "Then Acme signs off."], [0.3, 0.2])
    w.doc("menu.md", ["Lunch options near the office."], [0.0], kind="md")
    result = await w.find(file_refs=(ref(plan),))
    assert w.repo.knn_limits == [] and w.repo.calls["vectors"] == 1
    assert [f.item_id for f in result.files] == [plan] and len(result.files[0].chunks) == 2


async def test_file_refs_take_precedence_over_a_folder(w):
    """Confirmed choice: with both given, the files are the scope and the folder is not resolved."""
    rd = w.folder("R&D")
    w.doc("menu.md", ["Lunch options near the office."], [0.0], parent=rd, kind="md")
    plan = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.3])
    result = await w.find(folder="R&D", file_refs=(ref(plan),))
    assert [f.item_id for f in result.files] == [plan]
    assert result.folder_path == "" and w.repo.knn_limits == []


async def test_a_file_ref_with_no_indexed_text_is_counted(w):
    plan = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.3])
    rd = w.folder("R&D")
    empty = w.file("budget.ods", parent=rd, kind="ods")
    result = await w.find(file_refs=(ref(plan), ref(empty), ref("missing-1")))
    assert [f.item_id for f in result.files] == [plan]
    assert result.no_text == 2 and result.no_text_paths == ("R&D/budget.ods", ref("missing-1"))


# -- folder scope -----------------------------------------------------------------------------------------------------

async def test_a_folder_that_fits_ranks_in_memory(w):
    rd = w.folder("R&D")
    plan = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.3], parent=rd)
    w.doc("menu.md", ["Lunch options near the office."], [0.0], kind="md")
    result = await w.find(folder="R&D")
    assert w.repo.knn_limits == [] and w.repo.calls["vectors"] == 1
    assert [f.item_id for f in result.files] == [plan] and result.folder_path == "R&D"


async def test_a_folder_past_the_chunk_bound_runs_the_knn_over_the_walked_files_only(w, monkeypatch):
    """RFC Δ246: past FOLDER_INMEM_CHUNKS with a complete walk, one kNN over the walked readable files at CHUNK_FETCH
    (not the user-wide kNN at FETCH_MAX kept to the walk); no vectors are read in memory."""
    rd = w.folder("R&D")
    plan = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.3], parent=rd)
    w.set(plan, chunk_count=2_001)   # Σ chunk_count > FOLDER_INMEM_CHUNKS
    w.doc("menu.md", ["Lunch options near the office."], [0.0], kind="md")
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    result = await w.find(folder="R&D")
    assert w.repo.knn_limits == [] and w.repo.scoped_knn == [(CHUNK_FETCH, (plan,))] and w.repo.calls["vectors"] == 0
    assert [f.item_id for f in result.files] == [plan]
    lines = [c[0][0] for c in log.info.call_args_list if "[DriveSearch]" in c[0][0]]
    assert len(lines) == 1 and " scope=folder-scoped " in lines[0], lines


async def test_an_incomplete_walk_keeps_the_wide_hits_by_ancestry(w):
    rd = w.folder("R&D")
    for i in range(FOLDER_INMEM_CARDS + 1):
        w.repo.records[("u", f"x{i:05d}")] = DriveIndexRecord(
            user_id="u", item_id=f"x{i:05d}", parent_id=rd, name=f"x{i:05d}.txt", kind="txt", scope=Scope.NORMAL,
            state=RecordState.INDEXED, has_card=True)
    sub = w.folder("Drafts", parent=rd)
    plan = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.3], parent=sub)
    w.doc("menu.md", ["Lunch options near the office."], [0.0], kind="md")
    result = await w.find(folder="R&D")
    assert w.repo.knn_limits == [FETCH_MAX]
    assert [f.item_id for f in result.files] == [plan] and result.files[0].path == "R&D/Drafts/Tom's plan.docx"
    assert result.no_text == 0   # confirmed choice: an incomplete walk counts nothing


async def test_files_in_the_folder_with_no_indexed_text_are_counted(w):
    rd = w.folder("R&D")
    w.doc("Tom's plan.docx", ["The review is held weekly."], [0.3], parent=rd)
    w.file("budget.ods", parent=rd, kind="ods")
    w.folder("Drafts", parent=rd)
    result = await w.find(folder="R&D")
    assert result.no_text == 1 and result.no_text_paths == ("R&D/budget.ods",)


async def test_the_no_text_line_names_at_most_ten_paths_and_counts_all(w):
    """Confirmed choice: the count is the whole, the paths at most ten."""
    rd = w.folder("R&D")
    w.doc("Tom's plan.docx", ["The review is held weekly."], [0.3], parent=rd)
    for i in range(12):
        w.file(f"budget-{i:02d}.ods", parent=rd, kind="ods")
    result = await w.find(folder="R&D")
    assert result.no_text == 12 and len(result.no_text_paths) == 10


async def test_an_ambiguous_folder_returns_its_candidates(w):
    acme, old = w.folder("Acme"), w.folder("Old")
    w.folder("Specs", parent=acme)
    w.folder("Specs", parent=old)
    result = await w.find(folder="Specs")
    assert result.status == STATUS_AMBIGUOUS_FOLDER and result.files == ()
    assert result.folder_candidates == ("Acme/Specs", "Old/Specs")
    assert w.emb.calls == []


async def test_a_folder_not_found_answers_as_search_does(w):
    result = await w.find(folder="Nowhere")
    assert result.status == STATUS_FOLDER_NOT_FOUND and not result.files and w.emb.calls == []


async def test_an_opaque_folder_scope_is_not_indexed(w):
    code = w.folder("Code")
    w.set(code, scope=Scope.OPAQUE_ROOT)
    result = await w.find(folder="Code")
    assert result.status == STATUS_OK and result.folder_not_indexed and result.folder_path == "Code"


# -- the gate ---------------------------------------------------------------------------------------------------------

async def test_indexing_off_is_disabled(w):
    w.users.enabled["u"] = False
    result = await w.find()
    assert result.status == STATUS_DISABLED and w.emb.calls == []


async def test_a_purge_answers_building(w):
    w.repo.states["u"] = replace(w.repo.states["u"], purging=True)
    result = await w.find()
    assert result.status == STATUS_OK and result.building and not result.files and w.emb.calls == []


async def test_an_index_not_rebuilt_awaits_the_rebuild():
    w = PassageWorld(migrated=False)
    w.doc("Tom's plan.docx", ["The review is held weekly."], [0.0])
    result = await w.find()
    assert result.awaiting_rebuild and not result.files and w.emb.calls == [] and w.repo.knn_limits == []


async def test_another_drive_is_flagged(w):
    w.drive.connection_state = DriveConnection(connected=True, drive_id="d2")
    result = await w.find()
    assert result.status == STATUS_DISCONNECTED and result.other_drive


async def test_vectors_in_another_space_answer_wrong_space():
    w = PassageWorld(space="next-model/8")
    w.doc("Tom's plan.docx", ["The review is held weekly."], [0.0])
    result = await w.find()
    assert result.status == STATUS_OK and result.wrong_space and not result.awaiting_rebuild
    assert not result.files and w.emb.calls == [] and w.repo.knn_limits == []


# -- the file's marks -------------------------------------------------------------------------------------------------

async def test_card_accuracy_is_kept_only_for_a_scorable_record(w):
    briefed = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.1], card_accuracy=70,
                    brief=DocumentBrief(description="A plan for the review"))
    bare = w.doc("menu.md", ["The review lunch."], [0.2], kind="md", card_accuracy=70)
    result = await w.find()
    assert file_of(result, briefed).card_accuracy == 70
    assert file_of(result, bare).card_accuracy is None


async def test_a_stale_record_is_marked(w):
    plan = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.1], content_version="v2",
                 indexed_version="v1")
    assert file_of(await w.find(), plan).stale


# -- selection ----------------------------------------------------------------------------------------------------------

async def test_identical_copies_are_one_file_with_also_at(w):
    rd, old = w.folder("R&D"), w.folder("Old")
    first = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.1], parent=rd, content_hash="same:500")
    w.doc("Tom's plan.docx", ["The review is held weekly."], [0.2], parent=old, content_hash="same:500")
    result = await w.find()
    assert [f.item_id for f in result.files] == [first]
    assert result.files[0].also_at == ("Old/Tom's plan.docx",) and len(texts(result)) == 1


async def test_the_noted_copy_is_the_keeper(w):
    rd, old = w.folder("R&D"), w.folder("Old")
    w.doc("Tom's plan.docx", ["The review is held weekly."], [0.1], parent=rd, content_hash="same:500")
    noted = w.doc("Tom's plan.docx", ["The review is held weekly."], [0.2], parent=old, content_hash="same:500")
    w.note(noted, "the signed one")
    result = await w.find()
    assert [f.item_id for f in result.files] == [noted]
    assert result.files[0].also_at == ("R&D/Tom's plan.docx",)


async def test_shown_chunks_are_left_out_and_listed_as_repeated(w):
    plan = w.doc("Tom's plan.docx", ["The first review.", "The second review.", "The sign-off."], [0.1, 0.2, 0.3])
    first = await w.find()
    shown = frozenset(c.key for c in first.files[0].chunks if c.index == 0)
    again = await w.find(shown=shown)
    assert [c.index for c in again.files[0].chunks] == [1, 2]
    assert again.repeated == ((plan, 1, 0),)


async def test_a_chunk_longer_than_its_span_is_charged_at_its_text(w):
    """Confirmed choice: selection runs on span sizes, then again on the read texts' lengths (a continued table's
    chunk stores its header before the span), so the character cap counts the text that is shown."""
    plan = w.doc("Tom's plan.docx", ["x" * 6_000, "y" * 9_000, "z" * 9_000], [0.1, 0.2, 0.3])
    chunk, vector = w.repo.chunks[("u", plan, 1, 2)]
    w.repo.chunks[("u", plan, 1, 2)] = (replace(chunk, text="| step | owner |\n|---|---|\n" + chunk.text), vector)
    menu = w.doc("menu.md", ["Lunch options."], [0.4], kind="md")
    # spans sum to exactly 24 000, so the first selection takes all three and skips the menu; the third chunk's
    # text is 27 characters longer, so the re-run drops it and the menu's chunk fits
    result = await w.find()
    assert [c.index for c in file_of(result, plan).chunks] == [0, 1]
    assert [c.text for c in file_of(result, menu).chunks] == ["Lunch options."]
    assert sum(len(t) for t in texts(result)) <= EXCERPTS_CHARS_MAX
    assert w.repo.calls["texts"] == 2   # one read per file generation, the menu's after the re-run


# -- read by number -----------------------------------------------------------------------------------------------------

async def test_a_range_cut_at_the_size_limit_says_where_to_continue(w):
    long = "word " * 1_800   # 9 000 characters: two fit 24 000, a third does not
    plan = w.doc("Tom's plan.docx", [long] * 5, [0.1] * 5)
    result = await w.read(plan, 1, 5)
    assert [c.index for c in result.files[0].chunks] == [0, 1] and result.continue_at == 3
    assert w.repo.knn_limits == [] and w.emb.calls == []


async def test_a_range_past_the_end_is_clipped_without_a_continuation(w):
    plan = w.doc("Tom's plan.docx", ["a", "b", "c", "d", "e"], [0.1] * 5)
    result = await w.read(plan, 4, 9)
    assert [c.index for c in result.files[0].chunks] == [3, 4] and result.continue_at is None


async def test_a_range_reads_only_the_readable_generation(w):
    plan = w.doc("Tom's plan.docx", ["new first", "new second"], [0.1, 0.2], generation=2)
    w.stale_chunk(plan, 1, 0, "old first", 0.0)
    assert texts(await w.read(plan, 1, 2)) == ["new first", "new second"]


async def test_a_range_of_a_file_with_no_text_is_counted(w):
    rd = w.folder("R&D")
    empty = w.file("budget.ods", parent=rd, kind="ods")
    result = await w.read(empty, 1, 2)
    assert not result.files and result.no_text == 1 and result.no_text_paths == ("R&D/budget.ods",)


async def test_a_range_passes_the_gate(w):
    plan = w.doc("Tom's plan.docx", ["a"], [0.1])
    w.users.enabled["u"] = False
    assert (await w.read(plan, 1, 1)).status == STATUS_DISABLED


async def test_a_range_reads_while_vectors_are_in_another_space():
    """Confirmed choice: a read by number uses no vector, so the embedding space does not gate it."""
    w = PassageWorld(space="next-model/8")
    plan = w.doc("Tom's plan.docx", ["a", "b"], [0.1, 0.2])
    result = await w.read(plan, 1, 2)
    assert texts(result) == ["a", "b"] and not result.wrong_space


# -- the summary line ---------------------------------------------------------------------------------------------------

async def test_one_summary_line_with_counts_and_no_text(w, monkeypatch):
    w.doc("Tom's plan.docx", ["The secret review wording."], [0.1])
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    await w.find()
    lines = [c[0][0] for c in log.info.call_args_list if "[DriveSearch]" in c[0][0]]
    assert len(lines) == 1
    line = lines[0]
    assert re.search(r"mode=excerpts scope=all hits=1 candidates=1 keyed=1 selected=1 files=1 ", line), line
    assert re.search(r"\bms=\d+$", line) and "secret" not in line and QUESTION not in line
