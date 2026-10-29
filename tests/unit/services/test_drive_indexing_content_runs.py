"""Index runs in the tick (USER_DRIVE_INDEXING_RFC §8.1, §8.2, §7.4, §5.4, §8.5): the outcome classes and what each
leaves on the record and in the chunks."""
from dataclasses import replace

import pytest

from src.domain import drive_index as di
from src.domain.drive_card import card_text
from src.domain.drive_keys import word_keys_capped
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import Priority, Reason, RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.domain.drive_index_planner import backoff
from src.domain.drive_index_policy import size_cap
from src.domain.user_drive import DriveAccessDenied
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ContentWorld()


async def test_first_seen_document_is_indexed_and_carded_once(w):
    text = prose(120) + " Invoice INV-2024/0012 totals 1 250,00 EUR."
    doc = w.add_doc("lease.pdf", text)
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.reason, rec.indexed_generation, rec.indexed_version) == (RecordState.INDEXED, None, 1, "c1")
    assert rec.chunk_count == len(w.readable(doc)) > 1 and rec.extracted_by == "parser@1"
    # v2 (§25.2.7): no lead text in the card (the profile carries the content there, §25.4); chunks carry word keys
    assert rec.lead_text == "" and (rec.keys_version, rec.keys_pending) == (di.KEYS_VERSION, False)
    assert w.generations(doc) == [1]
    for chunk in w.readable(doc):
        assert chunk.kind == "pdf" and chunk.word_keys == word_keys_capped(chunk.text)[0]
        assert chunk.keys_version == di.KEYS_VERSION
    assert any("n:inv20240012" in c.word_keys for c in w.readable(doc))
    card_calls = [t for t in w.emb.calls if t.startswith("lease.pdf | ")]
    assert card_calls == [card_text(rec)]   # the card waited for the content: one embedding
    (request,) = w.parser.requests
    assert (request.kind, request.name, request.user_id, request.account_id) == ("pdf", "lease.pdf", "u", "a")
    assert request.path.endswith(".pdf") and request.max_model_pages == 0
    assert w.summary_total("indexed") == 1 and w.summary_total("chunks") == rec.chunk_count


async def test_temp_file_is_gone_after_the_run(w):
    w.add_doc("a.md", prose(5))
    await w.settle()
    import os
    assert not os.path.exists(w.parser.requests[0].path)


async def test_an_edit_commits_a_new_generation_and_collects_the_old(w):
    doc = w.add_doc("notes.md", prose(150))
    await w.settle()
    w.drive.edit(doc, prose(20, "rider").encode(), modified_at=w.clock.now)
    w.clock.advance(3600)
    await w.settle()
    rec = w.rec(doc)
    assert (rec.indexed_generation, rec.index_generation, rec.indexed_version) == (2, 2, rec.content_version)
    assert w.generations(doc) == [2] and all("rider" in c.text for c in w.readable(doc))


async def test_a_rename_costs_one_card_and_no_download(w):
    doc = w.add_doc("draft.md", prose(10))
    await w.settle()
    downloads, cards = len(w.drive.downloads), len(w.emb.calls)
    w.drive.rename(doc, "final.md")
    await w.settle()
    assert len(w.drive.downloads) == downloads and len(w.emb.calls) == cards + 1
    assert w.rec(doc).state is RecordState.INDEXED and w.generations(doc) == [1]


async def test_metadata_over_the_cap_is_never_downloaded(w):
    big = w.drive.add("huge.pdf", size_bytes=size_cap("pdf") + 1)
    await w.settle()
    assert w.rec(big).reason is Reason.TOO_LARGE and w.drive.downloads == []


async def test_a_stream_past_the_cap_is_too_large(w, monkeypatch):
    doc = w.add_doc("liar.md", prose(10))
    w.drive.items[doc] = replace(w.drive.items[doc], size_bytes=10)   # metadata understates the size
    monkeypatch.setattr(svc_mod, "size_cap", lambda kind: 50)
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.reason) == (RecordState.METADATA_ONLY, Reason.TOO_LARGE) and w.parser.requests == []


@pytest.mark.parametrize("status,reason", [(ExtractionStatus.NO_TEXT, Reason.NO_TEXT),
                                           (ExtractionStatus.ENCRYPTED, Reason.ENCRYPTED),
                                           (ExtractionStatus.UNSUPPORTED, Reason.KIND_NOT_ADMITTED),
                                           (ExtractionStatus.TOO_LARGE, Reason.TOO_LARGE)])
async def test_deterministic_outcomes_demote_once(w, status, reason):
    w.parser.outcomes["x.docx"] = ExtractionOutcome(status=status)
    doc = w.drive.add_file("x.docx", b"PK\x03\x04 body")
    await w.settle()
    w.clock.advance(RETRY_CAP.total_seconds() * 2)
    w.drive.rename(doc, "y.docx")
    w.parser.outcomes["y.docx"] = ExtractionOutcome(status=status)
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.reason, rec.indexed_generation) == (RecordState.METADATA_ONLY, reason, None)
    assert len(w.parser.requests) == 1 and rec.has_card


async def test_machine_text_is_demoted_and_keeps_its_lead_text(w):
    log = "\n".join(f"2026-10-01 12:00:{i % 60:02d},{i:03d} INFO [w-{i % 4}] GET /api/items/{i} 200 {i}ms"
                    for i in range(300))
    doc = w.add_doc("server.txt", log)
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.reason) == (RecordState.METADATA_ONLY, Reason.MACHINE_TEXT)
    assert rec.lead_text.startswith("2026-10-01") and rec.lead_text in card_text(rec) and w.chunks(doc) == []


async def test_a_table_is_not_machine_text(w):
    rows = "\n".join(f"| 2026-10-{i % 28 + 1:02d} | GET /api/items | 200 |" for i in range(300))
    w.parser.outcomes["log.csv"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=f"| d | r | s |\n|---|---|---|\n{rows}")
    doc = w.drive.add_file("log.csv", b"d,r,s\n")
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED


async def test_blank_text_is_no_text(w):
    w.parser.outcomes["blank.md"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=" \n\n ")
    doc = w.drive.add_file("blank.md", b"x")
    await w.settle()
    assert w.rec(doc).reason is Reason.NO_TEXT


async def test_truncation_reaches_the_record(w):
    w.parser.outcomes["book.epub"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(30), truncated=True)
    doc = w.drive.add_file("book.epub", b"PK\x03\x04")
    await w.settle()
    assert w.rec(doc).truncated


@pytest.mark.parametrize("status,reason", [(ExtractionStatus.ERROR, Reason.EXTRACT_ERROR),
                                           (ExtractionStatus.TIMEOUT, Reason.EXTRACT_TIMEOUT),
                                           (ExtractionStatus.MEMORY, Reason.EXTRACT_MEMORY)])
async def test_per_file_failures_back_off_then_fail_keeping_old_chunks(w, status, reason):
    doc = w.add_doc("a.md", prose(40))
    await w.settle()
    w.parser.outcomes["a.md"] = ExtractionOutcome(status=status)
    w.drive.edit(doc, prose(41).encode(), modified_at=w.clock.now)
    w.clock.advance(3600)
    for attempt in range(1, MAX_ATTEMPTS):
        await w.settle()
        rec = w.rec(doc)
        assert (rec.state, rec.attempts, rec.attempt_started_at) == (RecordState.PENDING, attempt, None)
        assert (rec.due_at, rec.priority) == (w.clock.now + backoff(attempt), Priority.RETRY)
        w.clock.advance(backoff(attempt).total_seconds())
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.reason, rec.indexed_generation) == (RecordState.FAILED, reason, 1) and rec.stale
    assert w.readable(doc) and w.generations(doc) == [1]
    assert len(w.parser.requests) == 1 + MAX_ATTEMPTS


async def test_a_download_error_is_retried_then_failed(w):
    doc = w.add_doc("a.md", prose(5))
    for _ in range(MAX_ATTEMPTS):
        w.drive.download_errors[doc] = DriveAccessDenied("denied")
        await w.settle()
        w.clock.advance(RETRY_CAP.total_seconds())
    rec = w.rec(doc)
    assert (rec.state, rec.reason) == (RecordState.FAILED, Reason.DOWNLOAD_ERROR) and w.parser.requests == []


async def test_an_extractor_exception_is_this_files_failure(w):
    def boom(request):
        raise RuntimeError("converter bug")
    w.parser.before = boom
    doc = w.add_doc("a.md", prose(5))
    other = w.add_doc("b.md", prose(5))
    w.parser.before = lambda r: (_ for _ in ()).throw(RuntimeError("bug")) if r.name == "a.md" else None
    result = await w.settle()
    assert result.status == "done"
    assert (w.rec(doc).attempts, w.rec(doc).state) == (1, RecordState.PENDING)
    assert w.rec(other).state is RecordState.INDEXED


async def test_a_sniffed_kind_the_policy_refuses_is_not_admitted(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "sniff_kind", lambda head, declared: "archive")
    doc = w.add_doc("really_a_zip.txt", "PK")
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.reason) == (RecordState.METADATA_ONLY, Reason.KIND_NOT_ADMITTED)
    w.drive.rename(doc, "renamed.txt")
    await w.settle()
    assert w.rec(doc).reason is Reason.KIND_NOT_ADMITTED and w.parser.requests == []


async def test_a_sniffed_admitted_kind_is_extracted_as_that_kind(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "sniff_kind", lambda head, declared: "pdf" if head.startswith(b"%PDF") else declared)
    doc = w.drive.add_file("scan.txt", b"%PDF-1.7\n" + prose(5).encode())
    await w.settle()
    (request,) = w.parser.requests
    assert request.kind == "pdf" and w.rec(doc).kind == "text"
    assert all(c.kind == "text" for c in w.readable(doc))   # chunks carry the record's kind (§3.3)


async def test_a_new_extraction_version_reindexes_to_fewer_chunks(w, monkeypatch):
    doc = w.add_doc("a.md", prose(200))
    await w.settle()
    first = w.rec(doc).chunk_count
    w.parser.outcomes["a.md"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(10))
    monkeypatch.setattr("src.domain.drive_index_planner.EXTRACTION_VERSION", di.EXTRACTION_VERSION + 1)
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {**di.CURRENT_VERSIONS, "extraction": di.EXTRACTION_VERSION + 1})
    await w.settle()
    rec = w.rec(doc)
    assert rec.indexed_generation == 2 and rec.chunk_count < first
    assert w.generations(doc) == [2] and len(w.readable(doc)) == rec.chunk_count
