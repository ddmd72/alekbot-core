"""Where the round-3 fixes meet in the tick (R3 merge; USER_DRIVE_INDEXING_RFC §5.4, §8.1, §10.2, §12.3, §24.7).

- Vector reuse (F1-3) meets re-extraction under new rules (an extractor or EXTRACTION_VERSION change, F5-5): the file
  is read again and no unchanged chunk is embedded again, but every chunk's exact terms are computed anew — reused
  vectors must never carry old terms along.
- The card slot (F1-2) meets the chunk cap read before the reader (F4-2): a file the slot admits and the chunk cap
  refuses is demoted with its card and no reader call; a file beyond the card cap is capped and never read either.
- The card cap's eviction (F1-2: an indexed record goes back to pending with no readable generation) meets the split
  unread counters (F5-2, refreshed every tick, F4-1): an evicted record is neither "not read yet" nor "could not be
  read" — the index-full line covers it.
- A chunk the new embedding space refuses (F2-3: indexed becomes failed) meets the same counters: it reads as could
  not be read, never as waiting."""
import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import Reason, RecordState, TriageVerdict
from src.domain.drive_index_params import MAX_CHUNKS_PER_USER, RETRY_CAP
from src.domain.drive_search import NOT_READ_YET_COUNTER, UNREAD_COUNTER
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .test_drive_indexing_reembed_refusal import NEW, RefusingEmbeddings, _switch_space, _world
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")

MARK = "zz-r3-merge-mark"


async def _counters(w: ContentWorld):
    return (await w.repo.get_sync_state(U_SCOPE)).counters


async def test_a_reextraction_reuses_every_vector_and_recomputes_every_chunks_terms(monkeypatch):
    """Search v2 (§25.2.2): a chunk's terms are its `word_keys`, computed by `word_keys_capped` (the v1 `content_terms`
    are retired); the property is unchanged: new keying rules reach every chunk of a re-extraction."""
    w = ContentWorld()
    doc = w.add_doc("terms.md", prose(300))
    await w.settle()
    before = w.rec(doc)
    texts = [c.text for c in w.readable(doc)]
    assert len(texts) > 4 and all(MARK not in c.word_keys for c in w.readable(doc))
    real_keys = svc_mod.word_keys_capped
    monkeypatch.setattr(svc_mod, "word_keys_capped",
                        lambda text: ((*real_keys(text)[0], MARK), False))   # the new rules
    w.parser._version = 2                                     # the extractor changed: its files are read again (§9)
    _switch_space(w, "test-space/8", w.emb)
    parses, embedded = len(w.parser.requests), len(w.emb.calls)
    await w.settle()
    after = w.rec(doc)
    assert len(w.parser.requests) == parses + 1
    assert after.state is RecordState.INDEXED and after.indexed_generation != before.indexed_generation
    assert [c.text for c in w.readable(doc)] == texts
    assert not set(texts) & set(w.emb.calls[embedded:])        # no unchanged chunk embedded again
    assert all(MARK in c.word_keys for c in w.readable(doc))
    assert w.generations(doc) == [after.indexed_generation]    # the old generation is collected


async def test_the_card_slot_and_the_chunk_cap_both_hold_before_the_reader(monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 2)
    w = ContentWorld(page_size=500)

    async def full(user_id):
        return MAX_CHUNKS_PER_USER

    w.repo.readable_chunk_total = full
    scans = []
    for n in range(3):
        w.parser.outcomes[f"scan{n}.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2)
        w.reader.outcomes[f"scan{n}.pdf"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(10),
                                                              pages_total=2, pages_sent=2, model_calls=1)
        scans.append(w.add_doc(f"scan{n}.pdf", ""))
    await w.settle()
    assert w.reader.requests == []
    demoted = [s for s in scans if w.rec(s).reason is Reason.USER_CHUNK_CAP]
    capped = [s for s in scans if w.rec(s).card_capped]
    assert len(demoted) == 2 and len(capped) == 1
    assert all(w.rec(s).state is RecordState.METADATA_ONLY and w.rec(s).has_card for s in demoted)
    held = w.rec(capped[0])
    assert (held.state, held.has_card, held.reason) == (RecordState.PENDING, False, None)
    assert len(w.parser.requests) == 2                         # the capped scan was not even parsed


async def test_an_evicted_record_is_neither_waiting_nor_unreadable_on_any_tick(monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 1)
    w = ContentWorld(page_size=500)
    img = w.add_image("receipt.jpg")
    w.triage.verdicts = {img: (TriageVerdict.TEXT, "")}
    w.reader.outcomes["receipt.jpg"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(20, "receipt"),
                                                         pages_total=1, pages_sent=1)
    await w.settle()
    assert w.rec(img).state is RecordState.INDEXED
    w.add_doc("lease.txt", prose(30, "lease"))                 # a document outranks an image (§5.4)
    for _ in range(20):
        before = len(w.queue.tasks)
        await w.tick()
        counters = await _counters(w)
        assert NOT_READ_YET_COUNTER not in counters and UNREAD_COUNTER not in counters, counters
        if not any(":cont:" in (t["key"] or "") for t in w.queue.tasks[before:]):
            break
        w.clock.advance(60)
    evicted = w.rec(img)
    assert evicted.card_capped and (evicted.state, evicted.indexed_generation) == (RecordState.PENDING, None)


async def test_a_chunk_the_new_space_refuses_reads_as_could_not_be_read():
    w = _world()
    doc = w.add_doc("notes.md", prose(150))
    await w.settle()
    assert UNREAD_COUNTER not in await _counters(w)
    refusing = RefusingEmbeddings(w.emb, [c.text for c in w.readable(doc)][:1])
    _switch_space(w, NEW, refusing)
    for _ in range(3):
        await w.settle(max_ticks=20)
        w.clock.advance(RETRY_CAP.total_seconds())
    assert (w.rec(doc).state, w.rec(doc).indexed_generation) == (RecordState.FAILED, None)
    counters = await _counters(w)
    assert counters.get(UNREAD_COUNTER) == 1 and NOT_READ_YET_COUNTER not in counters
