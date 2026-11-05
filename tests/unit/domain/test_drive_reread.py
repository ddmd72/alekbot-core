"""U5 frozen core (EXTERNAL_FILES_RFC §7 U5, A-U5-1): `same_read` is true only on an equal chunk sequence and equal
text-decided record fields, false on each single difference; `changed_chunks` names what Layer 1 would embed;
`reread_unchanged` moves only the stamp, the family, the extraction version and the run marks; `indexed` and `demoted`
copy the family; entering metadata_only from metadata clears it with the stamp."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_chunker import ChunkedText, TextChunk, chunk_text
from src.domain.drive_extraction import ExtractionOutcome
from src.domain.drive_index import (
    EXTRACTION_VERSION, DriveChunk, DriveIndexRecord, Reason, RecordState,
)
from src.domain.drive_index_planner import demoted, indexed, reread_unchanged
from src.domain.drive_reread import changed_chunks, same_read

NOW = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
SPACE = "gemini-embedding-2/768"
TEXT = ("Invoice FA-0137\n\nAcme to Alex Doe, 12 Oak Street, Springfield.\n\n"
        + "\n\n".join(f"Line {i}: lock cylinder and two key copies, 249,00 EUR." for i in range(120)))
CHUNKED = chunk_text(TEXT)


def stored_of(chunked: ChunkedText, generation: int = 4) -> list:
    return [DriveChunk(user_id="u1", item_id="i1", generation=generation, index=i, text=c.text,
                       char_start=c.char_start, char_end=c.char_end, kind="pdf")
            for i, c in enumerate(chunked.chunks)]


def record(**kw) -> DriveIndexRecord:
    base = dict(user_id="u1", connection_id="u1", item_id="i1", kind="pdf", state=RecordState.INDEXED,
                content_version="v7", indexed_version="v7", index_generation=5, indexed_generation=4,
                chunk_space=SPACE, chunk_count=len(CHUNKED.chunks), text_chars=len(TEXT), pages_total=2,
                truncated=False, script_rendered=False, capture="digital", capture_source="detected",
                extracted_by="parser@3", reader_family="", extraction_version=0,
                attempt_started_at=NOW - timedelta(seconds=30), attempts=1)
    base.update(kw)
    return DriveIndexRecord(**base)


def outcome(**kw) -> ExtractionOutcome:
    base = dict(status=ExtractionStatus.TEXT, text=TEXT, pages_total=2, truncated=False, extracted_by="parser@4",
                reader_family="pdf", capture="digital", capture_source="detected")
    base.update(kw)
    return ExtractionOutcome(**base)


def test_the_fixture_chunks_into_several_chunks():
    assert len(CHUNKED.chunks) >= 3 and not CHUNKED.truncated


def test_an_equal_sequence_is_the_same_read():
    assert same_read(record(), stored_of(CHUNKED), CHUNKED, outcome(), space=SPACE)


def test_stored_order_does_not_matter():
    assert same_read(record(), list(reversed(stored_of(CHUNKED))), CHUNKED, outcome(), space=SPACE)


def _with_chunk(chunked: ChunkedText, i: int, **kw) -> ChunkedText:
    chunks = list(chunked.chunks)
    chunks[i] = replace(chunks[i], **kw)
    return ChunkedText(chunks=tuple(chunks), truncated=chunked.truncated)


@pytest.mark.parametrize("case", [
    "text", "char_start", "char_end", "count_new", "count_stored", "count_record", "index_gap", "generation",
    "text_chars", "pages_total", "truncated_outcome", "truncated_chunker", "script_rendered", "capture",
    "capture_source", "space", "no_space", "edit", "never_indexed", "no_readable", "status_no_text",
])
def test_each_single_difference_is_not_the_same_read(case):
    rec, stored, chunked, out = record(), stored_of(CHUNKED), CHUNKED, outcome()
    if case == "text":
        chunked = _with_chunk(CHUNKED, 1, text=CHUNKED.chunks[1].text + ".")
    elif case == "char_start":
        chunked = _with_chunk(CHUNKED, 2, char_start=CHUNKED.chunks[2].char_start + 1)
    elif case == "char_end":
        chunked = _with_chunk(CHUNKED, 0, char_end=CHUNKED.chunks[0].char_end - 1)
    elif case == "count_new":
        chunked = ChunkedText(chunks=CHUNKED.chunks[:-1], truncated=False)
    elif case == "count_stored":
        stored = stored[:-1]
    elif case == "count_record":
        rec = record(chunk_count=len(CHUNKED.chunks) + 1)
    elif case == "index_gap":
        stored = [replace(c, index=c.index + 1) for c in stored]
    elif case == "generation":
        stored = stored_of(CHUNKED, generation=3)
    elif case == "text_chars":
        rec = record(text_chars=len(TEXT) + 1)
    elif case == "pages_total":
        out = outcome(pages_total=3)
    elif case == "truncated_outcome":
        out = outcome(truncated=True)
    elif case == "truncated_chunker":
        chunked = ChunkedText(chunks=CHUNKED.chunks, truncated=True)
    elif case == "script_rendered":
        out = outcome(script_rendered=True)
    elif case == "capture":
        out = outcome(capture="scan")
    elif case == "capture_source":
        out = outcome(capture_source="automatic")
    elif case == "space":
        rec = record(chunk_space="other-model/768")
    elif case == "no_space":
        rec = record(chunk_space="")
    elif case == "edit":
        rec = record(content_version="v8")
    elif case == "never_indexed":
        rec = record(indexed_version=None)
    elif case == "no_readable":
        rec = record(indexed_generation=None)
    elif case == "status_no_text":
        out = outcome(status=ExtractionStatus.NO_TEXT, text="")
    assert not same_read(rec, stored, chunked, out, space=SPACE)


def test_an_empty_record_is_never_the_same_read():
    empty = ChunkedText(chunks=(), truncated=False)
    assert not same_read(record(chunk_count=0, text_chars=0), [], empty, outcome(text=""), space=SPACE)


def test_changed_chunks_names_only_new_texts():
    assert changed_chunks(stored_of(CHUNKED), CHUNKED) == ()
    moved = _with_chunk(CHUNKED, 1, text="Total due 1.048,45 EUR")
    assert changed_chunks(stored_of(CHUNKED), moved) == (1,)
    # an offset alone embeds nothing new: the text is already stored
    assert changed_chunks(stored_of(CHUNKED), _with_chunk(CHUNKED, 2, char_start=0)) == ()
    extra = ChunkedText(chunks=CHUNKED.chunks + (TextChunk("Paid by alex@example.com", 0, 24),), truncated=False)
    assert changed_chunks(stored_of(CHUNKED), extra) == (len(CHUNKED.chunks),)


_RUN_MARKS = {"dirty_since", "attempts", "attempt_started_at", "defer_until", "defer_priority", "model_outages",
              "due_at", "priority"}


def test_reread_unchanged_moves_only_the_stamp_the_family_the_version_and_the_run_marks():
    before = record(profile_input_fingerprint="fp", passages_generation=4, lead_text="", has_card=True)
    after = reread_unchanged(before, outcome(), NOW)
    moved = {f.name for f in fields(DriveIndexRecord) if getattr(before, f.name) != getattr(after, f.name)}
    assert moved - _RUN_MARKS == {"extracted_by", "reader_family", "extraction_version"}
    assert (after.extracted_by, after.reader_family, after.extraction_version) == ("parser@4", "pdf",
                                                                                  EXTRACTION_VERSION)
    # the readable generation keeps serving; the started generation number stays burned
    assert after.indexed_generation == 4 and after.index_generation == 5 and after.chunk_count == before.chunk_count
    assert after.attempt_started_at is None and after.attempts == 0


def test_indexed_copies_the_family():
    done = indexed(record(), outcome=outcome(reader_family="docx"), generation=5, chunk_count=3, now=NOW,
                   chunk_space=SPACE)
    assert done.reader_family == "docx" and done.extracted_by == "parser@4"


def test_demoted_copies_the_family_of_its_outcome_and_keeps_the_record_s_without_one():
    rec = record(reader_family="eml")
    assert demoted(rec, Reason.MACHINE_TEXT, NOW, outcome=outcome(reader_family="csv")).reader_family == "csv"
    assert demoted(rec, Reason.NO_TEXT, NOW).reader_family == "eml"


def test_record_and_outcome_default_to_an_unknown_family():
    assert DriveIndexRecord(user_id="u", item_id="i").reader_family == ""
    assert ExtractionOutcome(status=ExtractionStatus.TEXT).reader_family == ""
