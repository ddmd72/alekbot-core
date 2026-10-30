"""The card cap is decided before content and triage (R3 F1-2, USER_DRIVE_INDEXING_RFC §5.4, §24.7): search drops a
content hit whose record has no card, so a file the cap refuses a card never pays for triage, the reader, the parser or
chunk embeddings. A capped record holds that work until it is released; a record evicted by a better-ranked one loses
its chunks, which stop counting toward the user's chunk cap."""
import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import RecordState, TriageVerdict
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ContentWorld(page_size=500)


def chunk_texts(w: ContentWorld):
    return [t for t in w.emb.calls if "payment terms" in t]


async def test_files_beyond_the_card_cap_are_never_read(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 2)
    docs = [w.add_doc(f"lease{i}.txt", prose(30, f"lease{i}")) for i in range(5)]
    await w.settle()
    read = [d for d in docs if w.rec(d).state is RecordState.INDEXED]
    held = [d for d in docs if w.rec(d).card_capped]
    assert len(read) == 2 and len(held) == 3
    assert len(w.parser.requests) == 2
    for d in held:
        rec = w.rec(d)
        assert (rec.state, rec.has_card, rec.due_at) == (RecordState.PENDING, False, None)
        assert w.chunks(d) == []
    assert await w.repo.readable_chunk_total(U_SCOPE) == sum(w.rec(d).chunk_count for d in read)


async def test_images_beyond_the_card_cap_are_never_triaged(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 2)
    for i in range(2):
        w.add_doc(f"lease{i}.txt", prose(30, f"lease{i}"))
    await w.settle()
    images = [w.add_image(f"IMG_{i}.jpg") for i in range(4)]
    await w.settle()
    assert w.triage.calls == [] and w.drive.thumbnail_calls == []
    assert all(w.rec(i).card_capped and w.rec(i).state is RecordState.PENDING for i in images)


async def test_an_evicted_record_loses_its_chunks_and_is_read_again_only_when_released(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 1)
    img = w.add_image("receipt.jpg")
    w.triage.verdicts = {img: (TriageVerdict.TEXT, "")}
    w.reader.outcomes["receipt.jpg"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(20, "receipt"),
                                                         pages_total=1, pages_sent=1)
    await w.settle()
    assert w.rec(img).state is RecordState.INDEXED and w.readable(img)
    doc = w.add_doc("lease.txt", prose(30, "lease"))   # a document outranks an image (§5.4)
    await w.settle()
    evicted = w.rec(img)
    assert evicted.card_capped and not evicted.has_card
    assert (evicted.state, evicted.indexed_generation) == (RecordState.PENDING, None)
    assert w.chunks(img) == []
    assert await w.repo.readable_chunk_total(U_SCOPE) == w.rec(doc).chunk_count
    reads = len(w.reader.requests)
    w.clock.advance(3600)
    await w.settle()
    assert len(w.reader.requests) == reads           # held while capped
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 10)
    w.drive.reemit(doc)                              # any tick with headroom releases it
    await w.settle()
    back = w.rec(img)
    assert not back.card_capped and back.has_card and back.state is RecordState.INDEXED and w.readable(img)
    assert len(w.reader.requests) == reads + 1
