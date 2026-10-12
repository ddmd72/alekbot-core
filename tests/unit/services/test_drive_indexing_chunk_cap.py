"""The per-user chunk cap and its release by headroom (USER_DRIVE_INDEXING_RFC §5.4, §17.1)."""
import pytest

from src.domain import drive_index_run
from src.domain.drive_card import card_text
from src.domain.drive_index import Reason, RecordState
from src.domain.drive_index_params import CHUNK_CHARS
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")
CAP = 10


def chunks_of(n):
    """A text the chunker cuts into exactly `n` chunks: paragraphs that each fill one."""
    return "\n\n".join(f"Paragraph {i}. " + "x" * (CHUNK_CHARS - 20) for i in range(n))


@pytest.fixture
def w(monkeypatch):
    monkeypatch.setattr(drive_index_run, "MAX_CHUNKS_PER_USER", CAP)
    monkeypatch.setattr(svc_mod, "MAX_CHUNKS_PER_USER", CAP)
    return ContentWorld()


async def test_a_file_past_the_cap_is_refused_whole(w):
    a = w.add_doc("a.md", chunks_of(6))
    await w.settle()
    b = w.add_doc("b.md", chunks_of(6))
    await w.settle()
    assert w.rec(a).chunk_count == 6 and len(w.readable(a)) == 6
    rec = w.rec(b)
    assert (rec.state, rec.reason, rec.chunk_count, rec.indexed_generation) == \
        (RecordState.METADATA_ONLY, Reason.USER_CHUNK_CAP, 6, None)
    assert w.chunks(b) == [] and rec.has_card and rec.lead_text == "" and card_text(rec).startswith("b.md")


async def test_a_refused_file_is_not_reextracted_while_it_cannot_fit(w):
    a = w.add_doc("a.md", chunks_of(6))
    await w.settle()
    b = w.add_doc("b.md", chunks_of(6))
    await w.settle()
    w.drive.edit(a, chunks_of(5).encode(), modified_at=w.clock.now)   # headroom 5 < 6
    w.clock.advance(3600)
    await w.settle()
    w.drive.rename(b, "b2.md")
    await w.settle()
    assert [r.name for r in w.parser.requests].count("b.md") == 1 and "b2.md" not in [r.name for r in w.parser.requests]
    assert w.rec(b).reason is Reason.USER_CHUNK_CAP


async def test_headroom_releases_the_refused_file(w):
    a = w.add_doc("a.md", chunks_of(6))
    await w.settle()
    b = w.add_doc("b.md", chunks_of(6))
    await w.settle()
    w.drive.delete(a)
    await w.settle()
    rec = w.rec(b)
    assert (rec.state, rec.chunk_count) == (RecordState.INDEXED, 6) and len(w.readable(b)) == 6
    assert w.summary_total("released_chunks") == 1


async def test_release_best_rank_first_and_within_headroom(w):
    a = w.add_doc("a.md", chunks_of(9))
    await w.settle()
    small = w.add_doc("small.md", chunks_of(2))
    big = w.add_doc("big.md", chunks_of(8))
    await w.settle()
    assert w.rec(small).state is RecordState.METADATA_ONLY and w.rec(big).state is RecordState.METADATA_ONLY
    w.drive.edit(a, chunks_of(7).encode(), modified_at=w.clock.now)   # headroom 3: only `small` fits
    w.clock.advance(3600)
    await w.settle()
    assert w.rec(small).state is RecordState.INDEXED and w.rec(big).reason is Reason.USER_CHUNK_CAP


async def test_an_edit_of_an_indexed_file_counts_its_old_generation_out(w):
    a = w.add_doc("a.md", chunks_of(8))
    await w.settle()
    w.drive.edit(a, chunks_of(10).encode(), modified_at=w.clock.now)
    w.clock.advance(3600)
    await w.settle()
    assert w.rec(a).state is RecordState.INDEXED and w.rec(a).chunk_count == 10
