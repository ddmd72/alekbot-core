"""A media decoder that cannot take the work (`ParserUnavailable`: the sandbox busy, unreachable, misconfigured, or lost
with no time left to retry) is systemic, never the recording's (UNTRUSTED_CONTENT_SANDBOX_RFC §7 S5, S5 design Δ8): from
`probe` or `extract_windows` it leaves the record and its job uncharged and unchanged, ends the tick without a
transcript step error, and the recording is transcribed as if nothing happened once the decoder is back. The real
DriveIndexingService and DriveTranscriptStage on the in-memory repository; synthetic recordings only."""
import logging

import pytest

from src.domain.drive_extraction import ParserUnavailable

from .drive_content_world import admitted  # noqa: F401  (fixture)
from .test_drive_indexing_transcripts import TranscriptWorld

pytestmark = pytest.mark.usefixtures("admitted")


def _no_step_error(caplog) -> bool:
    return not any("transcript extract step failed" in r.getMessage() for r in caplog.records)


async def test_an_unavailable_decoder_at_the_probe_charges_nothing_and_ends_the_tick(tmp_path, caplog):
    w = TranscriptWorld(tmp_path)
    audio = w.add_recording("Coimbra weekend voice memo.m4a")
    w.media.recordings[audio].probe_error = ParserUnavailable("busy")
    with caplog.at_level(logging.WARNING):
        results = [await w.tick() for _ in range(3)]
    assert all(r.status == "error" for r in results)                  # the tick ended, systemic
    rec = w.rec(audio)
    assert len(w.media.probes) == 3 and w.media.runs == []          # asked once per tick, never past the probe
    assert rec.transcript_state == "ready" and rec.transcript_attempts == 0 and rec.attempts == 0
    assert w.job(audio) is None and w.reserved() == 0.0 and w.batch.uploads == []
    assert _no_step_error(caplog)

    w.media.recordings[audio].probe_error = None                     # the sandbox is back
    await w.to_waiting(audio)
    assert w.rec(audio).transcript_attempts == 0


async def test_an_unavailable_decoder_at_the_extraction_keeps_the_job_and_charges_nothing(tmp_path, caplog):
    w = TranscriptWorld(tmp_path)
    audio = w.add_recording("Community garden meeting (Alex, Kim, Jordan).m4a")
    w.media.recordings[audio].extract_error = ParserUnavailable("unavailable")
    with caplog.at_level(logging.WARNING):
        result = await w.tick()
    assert result.status == "error" and "windows_extracted" not in result.summary
    rec = w.rec(audio)
    assert len(w.media.runs) == 1 and w.batch.uploads == []
    assert rec.transcript_attempts == 0 and rec.attempts == 0 and rec.transcript_state in ("ready", "working")
    job = w.job(audio)
    assert job is None or not job.windows                            # no window was recorded as extracted
    assert _no_step_error(caplog)

    w.media.recordings[audio].extract_error = None
    await w.to_waiting(audio)
    assert w.rec(audio).transcript_attempts == 0 and len(w.runner.submits) == 1
