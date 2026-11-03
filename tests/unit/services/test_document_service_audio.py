"""DocumentService.read on audio (P21): only a chat upload that is not a prefetch is transcribed, once per content
(cache by hash + model), metered and billed; everything else is the open path's audio note. Plus the failure paths
(each: a status AND an ERROR/WARNING log) and `transcription_cost` for both usage shapes."""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from src.domain.audio_transcript import AudioTranscript
from src.domain.billing import transcription_cost, transcription_cost_known
from src.domain.document_limits import MAX_FILE_BYTES
from src.domain.document_read import ReadRequest, ReadStatus, TRANSCRIPT_CACHE_DAYS
from src.ports.audio_transcription_port import AudioTranscriptionPort
from src.ports.quota_service import QuotaService
from src.ports.transcript_cache_port import TranscriptCachePort
from src.services.document_service import DocumentService
from tests.unit.services.test_document_service_read import FakeExtractor, FakeSplitter

AUDIO = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x01" * 64
TRANSCRIPT = "Lukas Weber owns the Q3 budget."
MODEL = "gpt-transcribe"


class FakeTranscriber(AudioTranscriptionPort):
    model = MODEL

    def __init__(self, answer: object = None, delay: float = 0.0) -> None:
        self.calls = 0
        self.answer = answer or AudioTranscript(text=TRANSCRIPT, model=MODEL, seconds=60.0)
        self.delay = delay

    async def transcribe(self, local_path, mime_type, languages=None):
        raise AssertionError("the service must use transcribe_metered")

    async def transcribe_metered(self, local_path, mime_type, languages=None) -> AudioTranscript:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class FakeCache(TranscriptCachePort):
    def __init__(self, fail: bool = False) -> None:
        self.entries: Dict[Tuple[str, str], Tuple[str, datetime]] = {}
        self.fail = fail

    async def get(self, account_id: str, key: str) -> Optional[str]:
        if self.fail:
            raise RuntimeError("cache down")
        entry = self.entries.get((account_id, key))
        return entry[0] if entry and entry[1] > datetime.now(timezone.utc) else None

    async def put(self, account_id: str, key: str, text: str, *, purge_after: datetime) -> None:
        if self.fail:
            raise RuntimeError("cache down")
        self.entries[(account_id, key)] = (text, purge_after)


class FakeQuota(QuotaService):
    def __init__(self, fail: bool = False) -> None:
        self.bills: List[Tuple[str, str, int, float]] = []
        self.fail = fail

    async def record_usage(self, account_id: str, model: str, tokens: int, cost: float) -> None:
        if self.fail:
            raise RuntimeError("quota down")
        self.bills.append((account_id, model, tokens, cost))


def build(transcriber="default", cache="default", quota="default"):
    transcriber = FakeTranscriber() if transcriber == "default" else transcriber
    cache = FakeCache() if cache == "default" else cache
    quota = FakeQuota() if quota == "default" else quota
    service = DocumentService(FakeExtractor("parser"), FakeExtractor("reader"), FakeSplitter(),
                              transcriber=transcriber, transcript_cache=cache, quota=quota)
    return service, transcriber, cache, quota


def req(store="chat", caller="open", deadline=60.0, content=AUDIO) -> ReadRequest:
    return ReadRequest(content=content, name="voice_call.m4a", declared_mime="audio/wav", store=store, caller=caller,
                       native_formats=(), user_id="ann", account_id="ann", deadline_at=time.time() + deadline,
                       request_id="r1")


async def test_chat_audio_is_transcribed_once_and_billed_once():
    service, transcriber, cache, quota = build()
    first = await service.read(req())
    second = await service.read(req())
    assert first.status is second.status is ReadStatus.TEXT and first.text == second.text == TRANSCRIPT
    assert first.links == ("transcribe-openai",) and second.links == ("transcribe-cache",)
    assert first.model_calls == 1 and first.model_cost_usd == pytest.approx(0.0045) and second.model_calls == 0
    assert transcriber.calls == 1 and quota.bills == [("ann", MODEL, 0, pytest.approx(0.0045))]
    (text, purge_after), = cache.entries.values()
    assert timedelta(days=TRANSCRIPT_CACHE_DAYS - 1) < purge_after - datetime.now(timezone.utc) <= timedelta(days=TRANSCRIPT_CACHE_DAYS)
    assert first.kind == "audio"


@pytest.mark.parametrize("store,caller", [("drive", "open"), ("workspace", "open"), ("delivered", "open"),
                                          ("email", "open"), ("chat", "prefetch"), ("drive", "prefetch")])
async def test_other_paths_and_any_prefetch_are_unsupported_with_no_call(store, caller, caplog):
    service, transcriber, cache, quota = build()
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(store=store, caller=caller))
    assert out.status is ReadStatus.UNSUPPORTED and out.detail == "audio"
    assert transcriber.calls == 0 and quota.bills == [] and cache.entries == {}
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_no_transcriber_is_unsupported():
    service, *_ = build(transcriber=None)
    out = await service.read(req())
    assert out.status is ReadStatus.UNSUPPORTED and out.detail == "audio"


async def test_audio_over_the_file_cap_is_too_large_with_no_call():
    service, transcriber, *_ = build()
    out = await service.read(req(content=AUDIO + b"x" * MAX_FILE_BYTES))
    assert out.status is ReadStatus.TOO_LARGE and transcriber.calls == 0


async def test_a_transcription_past_the_deadline_is_a_timeout(caplog):
    service, *_ = build(transcriber=FakeTranscriber(delay=5.0))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(deadline=0.1))
    assert out.status is ReadStatus.TIMEOUT and out.detail == "deadline"
    assert any(r.levelno == logging.WARNING and "timeout" in r.getMessage() for r in caplog.records)


async def test_a_failed_transcription_is_an_error_and_is_logged_and_not_billed(caplog):
    service, transcriber, cache, quota = build(transcriber=FakeTranscriber(RuntimeError("provider said no")))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req())
    assert out.status is ReadStatus.ERROR and "RuntimeError" in out.detail and "provider said no" not in out.detail
    assert quota.bills == [] and cache.entries == {}
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_a_cache_failure_never_fails_the_open(caplog):
    service, transcriber, _, quota = build(cache=FakeCache(fail=True))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req())
    assert out.status is ReadStatus.TEXT and out.text == TRANSCRIPT and len(quota.bills) == 1
    assert sum("transcript cache" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records) == 2


async def test_a_quota_failure_keeps_the_text_and_is_logged_as_an_error(caplog):
    service, *_ = build(quota=FakeQuota(fail=True))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req())
    assert out.status is ReadStatus.TEXT and out.model_cost_usd > 0
    assert any(r.levelno == logging.ERROR and "lost" in r.getMessage() for r in caplog.records)


async def test_a_token_shaped_usage_without_a_published_rate_is_flagged(caplog):
    tokens = AudioTranscript(text=TRANSCRIPT, model=MODEL, input_tokens=100, output_tokens=20)
    service, _, _, quota = build(transcriber=FakeTranscriber(tokens))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req())
    assert out.status is ReadStatus.TEXT and quota.bills == [("ann", MODEL, 120, 0.0)]
    assert any(r.levelno == logging.WARNING and "billed at 0.0" in r.getMessage() for r in caplog.records)


def test_transcription_cost_for_both_usage_shapes():
    assert transcription_cost(AudioTranscript(text="", model=MODEL, seconds=120.0)) == pytest.approx(0.009)
    assert transcription_cost_known(AudioTranscript(text="", model=MODEL, seconds=120.0))
    tokens = AudioTranscript(text="", model=MODEL, input_tokens=1000, output_tokens=10)
    assert transcription_cost(tokens) == 0.0 and not transcription_cost_known(tokens)
    unknown = AudioTranscript(text="", model="whisper-9", seconds=60.0)
    assert transcription_cost(unknown) == 0.0 and not transcription_cost_known(unknown)
    assert transcription_cost(AudioTranscript(text="", model=MODEL)) == 0.0
