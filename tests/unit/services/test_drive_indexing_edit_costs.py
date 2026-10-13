"""Edits do not re-pay for what did not change (final review F1-3; USER_DRIVE_INDEXING_RFC §7.3, §8.1, §24.9):
- an index run reuses the vector of every chunk whose text the readable generation already holds, when that
  generation is in the same embedding space; only new or changed chunks are embedded;
- a file the reader read is not read by it again within MODEL_REREAD_COOLDOWN: the edits made meanwhile wait, and the
  newest version is read once the cooldown ends. A file only the parser reads is not held."""
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import RecordState
from src.domain.drive_index_params import MODEL_REREAD_COOLDOWN

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")


def _edit(w, doc, text: str) -> None:
    w.drive.edit(doc, text.encode(), modified_at=w.clock.now - timedelta(hours=1))   # its quiet period is over


async def test_an_edit_embeds_only_the_changed_chunks():
    w = ContentWorld()
    base = prose(300)
    doc = w.add_doc("terms.md", base)
    await w.settle()
    count = w.rec(doc).chunk_count
    assert count > 4
    calls = len(w.emb.calls)
    _edit(w, doc, base + " A rider was signed on the last day.")
    await w.settle()
    rec = w.rec(doc)
    embedded = w.emb.calls[calls:]
    chunk_texts = [c.text for c in w.readable(doc)]
    assert rec.state is RecordState.INDEXED and len(chunk_texts) == rec.chunk_count
    content = [t for t in embedded if t in chunk_texts]
    assert 1 <= len(content) <= 2   # the last chunk changed; the rest kept their vectors
    for chunk in w.readable(doc):   # every readable chunk carries the vector of its own text
        _, vector = w.repo.chunks[("u", doc, chunk.generation, chunk.index)]
        assert vector == await w.emb.get_embedding(chunk.text)


async def test_vectors_of_another_space_are_never_reused():
    w = ContentWorld()
    base = prose(300)
    doc = w.add_doc("terms.md", base)
    await w.settle()
    w.repo.records[("u", doc)] = replace(w.rec(doc), chunk_space="old-space/8")
    calls = len(w.emb.calls)
    _edit(w, doc, base + " A rider was signed on the last day.")
    await w.settle()
    chunk_texts = {c.text for c in w.readable(doc)}
    assert chunk_texts <= set(w.emb.calls[calls:])   # every chunk embedded afresh


SCAN = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=3)


def _read(text):
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text=text, pages_total=3, pages_sent=3, model_calls=1)


async def test_a_model_read_file_is_read_again_only_after_the_cooldown():
    w = ContentWorld()
    w.parser.outcomes["scan.pdf"] = SCAN
    w.reader.outcomes["scan.pdf"] = _read("Invoice total 12.50 EUR, paid in full.")
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    read_at = w.clock.now
    assert len(w.reader.requests) == 1 and w.rec(doc).state is RecordState.INDEXED
    for hour in range(1, 6):   # edited every hour: none of them is read by the model yet
        w.clock.advance(3600)
        _edit(w, doc, f"%PDF-1.7\nversion {hour}")
        await w.settle()
    assert len(w.reader.requests) == 1
    rec = w.rec(doc)
    assert rec.state is RecordState.PENDING and rec.due_at == read_at + MODEL_REREAD_COOLDOWN
    w.reader.outcomes["scan.pdf"] = _read("Invoice total 99.00 EUR, paid in full.")
    w.clock.now = read_at + MODEL_REREAD_COOLDOWN
    await w.settle()
    rec = w.rec(doc)
    assert len(w.reader.requests) == 2
    assert rec.state is RecordState.INDEXED and rec.indexed_version == rec.content_version   # the newest version


async def test_a_parser_file_is_not_held():
    w = ContentWorld()
    doc = w.add_doc("notes.md", prose(20))
    await w.settle()
    for hour in range(1, 4):
        w.clock.advance(3600)
        _edit(w, doc, prose(20 + hour))
        await w.settle()
        rec = w.rec(doc)
        assert rec.state is RecordState.INDEXED and rec.indexed_version == rec.content_version


async def test_a_reader_retry_of_the_same_version_is_not_held():
    w = ContentWorld()
    w.parser.outcomes["scan.pdf"] = SCAN
    w.reader.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.ERROR, pages_total=3, pages_sent=3,
                                                      model_calls=1)
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    assert w.rec(doc).state is RecordState.PENDING and w.rec(doc).attempts == 1
    w.reader.outcomes["scan.pdf"] = _read("Invoice total 12.50 EUR, paid in full.")
    w.clock.advance(3600)   # past the retry backoff, far within the cooldown
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED and len(w.reader.requests) == 2
