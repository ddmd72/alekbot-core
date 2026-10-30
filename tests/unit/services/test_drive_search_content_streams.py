"""DriveSearchService with the content streams (USER_DRIVE_INDEXING_RFC §7.4, §10.1 step 3, §10.2, §17.4, §17.6,
§24.6). The records come from the real tick (metadata only); chunks are written straight into the repository and the
records marked readable, as the index run (plan part B) will. The policy admits content by monkeypatch."""
from datetime import datetime, timezone

import pytest

from src.domain import drive_index_policy as policy
from src.domain.drive_content_terms import content_terms
from src.domain.drive_index import DriveChunk, RecordState
from src.domain.drive_search import render_results

from .test_drive_search_service import SpyRepo, World

MARCH = dict(date_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
             date_to=datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc))
FAR = [-1.0] * 8


class ContentSpy(SpyRepo):
    """Records the content-stream calls; stream reads come back without text, as Firestore's projection does."""

    def __init__(self) -> None:
        super().__init__()
        self.chunk_limits = []
        self.term_calls = []
        self.window_calls = []

    async def find_nearest_chunks(self, user_id, vector, limit, kind=None):
        self.chunk_limits.append((limit, kind))
        hits = await super().find_nearest_chunks(user_id, vector, limit, kind)
        return [(_blank(c), d) for c, d in hits]

    async def find_chunks_by_term(self, user_id, term, limit, kind=None):
        self.term_calls.append((term, limit, kind))
        return [_blank(c) for c in await super().find_chunks_by_term(user_id, term, limit, kind)]

    async def get_chunks(self, user_id, item_id, generation, indexes):
        self.window_calls.append((item_id, generation, tuple(indexes)))
        return await super().get_chunks(user_id, item_id, generation, indexes)


def _blank(c):
    return DriveChunk(c.user_id, c.item_id, c.generation, c.index, "", c.char_start, c.char_end, c.kind,
                      c.content_terms, connection_id=c.connection_id)


@pytest.fixture(autouse=True)
def admit(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", policy.SLICE2_CONTENT_KINDS)


def world(**kw):
    w = World(**kw)
    spy = ContentSpy()
    w.repo = spy
    w.indexer._repo = spy
    w.search_svc._repo = spy
    return w


async def settle_metadata(w, monkeypatch):
    """The Slice 1 tick builds the records; the content kinds are admitted only for the search."""
    monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
    await w.settle()
    monkeypatch.setattr(policy, "ADMITTED_KINDS", policy.SLICE2_CONTENT_KINDS)


async def query_vector(w, query):
    return await w.emb.get_embedding(query, task_type="RETRIEVAL_QUERY")


async def index(w, item_id, texts, vectors, gen=1, readable=True):
    """Write one generation of chunks (with real offsets and content terms); mark it readable unless told not to."""
    rec = w.repo.records[("u", item_id)]
    chunks, start = [], 0
    for i, text in enumerate(texts):
        chunks.append(DriveChunk("u", item_id, gen, i, text, start, start + len(text), rec.kind, content_terms(text),
                                 connection_id="u"))
        start += len(text)
    await w.repo.write_chunks(chunks, vectors)
    if readable:
        w.set_state(item_id, state=RecordState.INDEXED, index_generation=gen, indexed_generation=gen,
                    indexed_version=rec.content_version, chunk_count=len(texts))


def by_id(result):
    return {c.item_id: c for c in result.cards}


# --- the chunk stream, the window and `matched` -----------------------------------------------------------------


async def test_content_streams_do_not_run_while_nothing_is_admitted(monkeypatch):
    w = world()
    scan = w.drive.add("Scan_0012.pdf")
    await settle_metadata(w, monkeypatch)
    await index(w, scan, ["Invoice INV-2024/0012"], [await query_vector(w, "INV-2024/0012")])
    monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
    monkeypatch.setattr(policy, "TRIAGED_KINDS", frozenset())   # since the flip images are triaged (§24.1)
    result = await w.search("INV-2024/0012")
    assert w.repo.chunk_limits == [] and w.repo.term_calls == [] and w.repo.window_calls == []
    assert not result.envelope.content_search and all(c.passage == "" for c in result.cards)


# --- the read fence (§7.4, §17.4) ----------------------------------------------------------------------------


async def test_a_demoted_or_deleting_record_shows_no_content(monkeypatch):
    w = world()
    demoted, deleting = w.drive.add("a.pdf"), w.drive.add("b.pdf")
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "secret clause")
    await index(w, demoted, ["DEMOTED TEXT"], [q])
    await index(w, deleting, ["DELETING TEXT"], [q])
    w.set_state(demoted, state=RecordState.METADATA_ONLY, indexed_generation=None)
    w.set_state(deleting, state=RecordState.DELETING)
    result = await w.search("secret clause")
    text = render_results(result)
    assert "DEMOTED TEXT" not in text and "DELETING TEXT" not in text
    assert all(c.passage == "" for c in result.cards)   # v2 field (P6 review m12): no content shown


# --- pooling before RRF (§17.6) and the starving document ----------------------------------------------------


# --- the exact-term stream (§24.6) ----------------------------------------------------------------------------


# --- folder scope (§10.1 Folder) -------------------------------------------------------------------------------


# --- the summary line ----------------------------------------------------------------------------------------


# --- the window budget (WINDOW_CHARS_TOTAL) --------------------------------------------------------------------

