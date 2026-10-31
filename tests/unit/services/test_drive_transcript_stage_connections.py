"""The transcript stage per connection (MULTI_DRIVE_RFC §8, §10, I8, E24): the orphan sweep never deletes a head a
disconnect retained or another connection's record carries; the media sandbox only ever receives a header-free media
source. Synthetic data, technical ids."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from src.domain.drive_index_params import TRANSCRIPT_ORPHAN_TTL
from src.domain.drive_transcript import RETAINED_DISCONNECT, Segment, StitchedTranscript, StoredTranscript
from src.services.drive_transcript_stage import DriveTranscriptStage

from .drive_transcript_fakes import (
    HANDLE, NOW, USER, Clock, FakeBatch, FakeDrive, FakeHost, FakeMedia, FakeRunner, Recording, record,
)

T = StitchedTranscript((Segment(0.0, None, "x"),), ("uk",), 1, 60.0)
LONG_AGO = NOW - TRANSCRIPT_ORPHAN_TTL - timedelta(days=1)


def _env(tmp_path):
    clock = Clock()
    from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
    repo = InMemoryDriveIndexRepository()
    media, batch, runner, drive = FakeMedia(clock), FakeBatch(clock), FakeRunner(), FakeDrive()
    host = FakeHost(repo, clock)
    stage = DriveTranscriptStage(repo, drive, media, batch, runner, clock=clock, monotonic=clock.monotonic,
                                 monthly_cap_usd=20.0, temp_root=str(tmp_path))
    return SimpleNamespace(clock=clock, repo=repo, media=media, drive=drive, host=host, stage=stage)


async def _cheap(env):
    return await env.stage.cheap_steps(HANDLE, env.host, stop_at=env.clock.mono + 600.0)


async def test_a_retained_head_is_never_swept(tmp_path):
    """I8: a head a disconnect retained outlives the TTL; it is touched like a kept one, so it leaves the sweep's page."""
    env = _env(tmp_path)
    await env.repo.put_transcript(USER, StoredTranscript("paid", T, last_used_at=LONG_AGO))
    await env.repo.retain_transcript(USER, "paid", LONG_AGO, RETAINED_DISCONNECT)
    summary = await _cheap(env)
    assert summary["transcript_orphans_kept"] == 1
    kept = env.repo.transcripts[(USER, "paid")]
    assert kept.retained_at == LONG_AGO and kept.last_used_at == NOW


async def test_a_head_another_connections_record_carries_is_kept(tmp_path):
    """The store is the user's: a record of any connection keeps the head."""
    env = _env(tmp_path)
    await env.repo.put_transcript(USER, StoredTranscript("shared", T, last_used_at=LONG_AGO))
    other = replace(record("b"), connection_id="conn-2", transcript_key="shared", transcript_state="current")
    await env.repo.upsert_records([other])
    summary = await _cheap(env)
    assert summary["transcript_orphans_kept"] == 1 and (USER, "shared") in env.repo.transcripts


async def test_the_sandbox_never_receives_a_source_that_needs_headers(tmp_path):
    """E24: a media source with request headers would carry a provider credential into the network-capable sandbox:
    the file is charged an attempt and nothing reaches the media extractor."""
    env = _env(tmp_path)
    env.media.recordings["a"] = Recording(600)
    env.drive.headers = {"Authorization": "Bearer secret"}
    rec = record("a", transcript_state="ready")
    await env.repo.upsert_records([rec])
    summary = await env.stage.extract_step(HANDLE, env.host, stop_at=env.clock.mono + 600.0)
    assert summary.get("transcript_attempts_charged") == 1
    assert env.media.probes == [] and env.media.runs == []
    assert env.repo.transcript_jobs.get((USER, "a")) is None or not env.repo.transcript_jobs[(USER, "a")].windows


async def test_a_header_free_source_reaches_the_sandbox_as_its_url(tmp_path):
    env = _env(tmp_path)
    env.media.recordings["a"] = Recording(600)
    await env.repo.upsert_records([record("a", transcript_state="ready")])
    await env.stage.extract_step(HANDLE, env.host, stop_at=env.clock.mono + 600.0)
    assert env.media.probes and all(url.startswith("https://") for url in env.media.probes)
    job = env.repo.transcript_jobs[(USER, "a")]
    assert job.connection_id == USER and job.user_id == USER   # the job is the connection's document
