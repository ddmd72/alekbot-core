"""A live transcript over the cap carries the open bound's cut line (USER_DRIVE_INDEXING_RFC §29.10)."""
from unittest.mock import AsyncMock

from src.domain import drive_open_texts as T
from src.domain.drive_index import DriveIndexRecord, RecordState
from src.domain.drive_index_params import OPEN_MAX_CHARS
from src.domain.drive_text_extent import ExtentWhy
from src.domain.drive_transcript import Segment, StitchedTranscript, StoredTranscript
from src.ports.drive_index_repository import DriveIndexRepository
from src.services.drive_search_service import DriveSearchService


def _service(rec, stored):
    repo = AsyncMock(spec=DriveIndexRepository)
    repo.get_records.return_value = {rec.item_id: rec}
    repo.get_transcript.return_value = stored
    return DriveSearchService(repo, AsyncMock(), AsyncMock(), AsyncMock(), embedding_space="s/8",
                              nonce=lambda: "nonce0001")


def _rec(**kw):
    base = dict(user_id="u", item_id="a1", name="call.m4a", state=RecordState.INDEXED,
                transcript_state="current", transcript_key="k1")
    base.update(kw)
    return DriveIndexRecord(**base)


def _long():
    segs = tuple(Segment(float(i * 10), None, f"Clause {i}. The tenant keeps the garage gate closed after 23:00. " * 8)
                 for i in range(400))
    return StoredTranscript("k1", StitchedTranscript(segments=segs, language_codes=("en",), speakers=1,
                                                     duration_s=4000.0))


async def test_an_unindexed_live_transcript_gets_the_not_indexed_line():
    text = await _service(_rec(), _long()).transcript_text("u", "a1")
    assert "[Transcript cut at" not in text
    assert T.TRANSCRIPT_SUBJECT in text and T.WHY[ExtentWhy.NO_TEXT] in text
    assert len(text) < OPEN_MAX_CHARS + 2_000


async def test_the_pointer_names_the_mirror_path():
    svc = _service(_rec(), _long())
    svc._active_mirror_path = AsyncMock(return_value="_Transcripts/Oak Street call.md")
    text = await svc.transcript_text("u", "a1")
    assert "The full text is in _Transcripts/Oak Street call.md." in text


async def test_a_pageable_record_picks_the_indexed_form():
    rec = _rec(indexed_generation=1, chunk_count=40, text_chars=500_000, content_version="c", indexed_version="c")
    text = await _service(rec, _long()).transcript_text("u", "a1")
    assert "can be read through the index" in text
    assert T.WHY[ExtentWhy.NO_TEXT] not in text


async def test_a_stale_record_does_not_claim_pages():
    rec = _rec(indexed_generation=1, chunk_count=40, text_chars=500_000, content_version="new", indexed_version="old")
    text = await _service(rec, _long()).transcript_text("u", "a1")
    assert "can be read through the index" not in text
