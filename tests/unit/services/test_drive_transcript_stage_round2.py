"""Fix round 2 of the final review. (1) A lookup that hit the provider's scan bound is inconclusive: it never counts toward
the miss limit, so the money is never released while the batch may exist. (2) A header that understated the duration: the
measured duration reserves the difference before any upload, or the file is parked mid-job (Δ106/T-R26, T-R21)."""
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain.drive_index_params import BATCH_SUBMIT_LOOKUP_MIN_AGE_S, BATCH_SUBMIT_LOOKUPS
from src.domain.drive_transcript_plan import file_estimate_usd
from src.domain.drive_index_params import BATCH_ERROR_BACKOFF_S
from src.domain.exceptions import BatchLookupInconclusive, LLMUnavailableError
from tests.unit.services import test_drive_transcript_stage_extract as ext
from tests.unit.services import test_drive_transcript_stage_final_f1 as f1
from tests.unit.services.drive_transcript_fakes import USER, FakeMedia, Recording, record


async def test_an_inconclusive_lookup_never_counts_toward_the_miss_limit(tmp_path):
    env = f1._env(tmp_path)
    await f1._add(env, record("a"), a=Recording(600))
    env.runner.raise_next = LLMUnavailableError("connection reset")
    await f1._extract(env)
    ((_, pending),) = env.repo.batches.items()
    env.batch.find_error = BatchLookupInconclusive("find: scan bound")
    env.clock.now = pending.created_at + timedelta(seconds=BATCH_SUBMIT_LOOKUP_MIN_AGE_S * 10)
    for _ in range(BATCH_SUBMIT_LOOKUPS + 3):
        f1._due(env, env.repo.batches[(USER, pending.name)])
        await f1._cheap(env)
    assert env.repo.batches[(USER, pending.name)].lookups == 0
    assert env.host.releases == []                       # the money is kept
    window = f1._job(env, "a").windows[0]
    assert window.state == "submitted"                   # the windows are not offered for a second purchase


async def test_an_inconclusive_lookup_does_not_starve_the_other_batches(tmp_path):
    env = f1._env(tmp_path)
    await f1._add(env, record("a"), a=Recording(600))
    env.runner.raise_next = LLMUnavailableError("connection reset")
    await f1._extract(env)                                   # a: a `submitting` document with an unknown outcome
    await f1._add(env, record("b"), b=Recording(600))
    await f1._extract(env)                                   # b: a normal batch
    (open_b,) = [x for x in f1._batches(env) if x.state == "submitted"]
    (pending,) = [x for x in f1._batches(env) if x.state == "submitting"]
    f1._answer(env, open_b)
    env.clock.now = max(pending.next_poll_at, open_b.next_poll_at)
    env.batch.find_error = BatchLookupInconclusive("find: scan bound")
    outages_before = list(env.host.outages)
    pending_reserved = pending.reserved_usd
    summary = await f1._cheap(env)
    assert summary["batch_lookup_inconclusive"] == 1
    assert (await f1._get(env, "b")).transcript_state == "current"       # the other batch was collected in this step
    assert env.host.outages == outages_before                             # the transcriber is not backed off
    kept = env.repo.batches[(USER, pending.name)]
    assert kept.lookups == 0 and kept.reserved_usd == pending_reserved   # no miss counted, the money held
    assert kept.next_poll_at == env.clock.now + timedelta(seconds=BATCH_ERROR_BACKOFF_S)


class UnderstatedMedia(FakeMedia):
    """The header says 600 s; the extraction reads to the end and measures `real_s` (M2)."""
    real_s = 1500.0

    async def extract_windows(self, *args, **kwargs):
        out = await super().extract_windows(*args, **kwargs)
        return replace(out, eof=True, duration_s=self.real_s)


def _swap_media(env):
    media = UnderstatedMedia(env.clock, seconds_per_window=30.0)
    media.recordings = env.media.recordings
    env.media = media
    env.stage._media = media


async def test_an_understated_header_reserves_the_difference_before_the_upload(tmp_path):
    env = ext._env(tmp_path)
    _swap_media(env)
    await ext._add(env, record("a"), a=Recording(600))
    reserved_before_upload = []
    real_upload = env.batch.upload_audio

    async def upload(path, mime):
        reserved_before_upload.append(ext._reserved(env))
        return await real_upload(path, mime)
    env.batch.upload_audio = upload
    await ext._step(env)
    want = file_estimate_usd(1500.0)
    assert reserved_before_upload and reserved_before_upload[0] == pytest.approx(want)
    job = ext._job(env, "a")
    assert job.reserved_usd == pytest.approx(want) and job.duration_s == 1500.0
    assert ext._reserved(env) == pytest.approx(want)    # booked once, not twice


async def test_an_understated_header_that_does_not_fit_parks_the_file_before_any_upload(tmp_path):
    low, high = file_estimate_usd(600.0), file_estimate_usd(1500.0)
    env = ext._env(tmp_path, cap=(low + high) / 2)
    _swap_media(env)
    await ext._add(env, record("a"), a=Recording(600))
    await ext._step(env)
    rec = await ext._get(env, "a")
    assert rec.transcript_state == "parked" and rec.transcript_hold == "resubmit"
    assert env.batch.uploads == []
    assert ext._reserved(env) == pytest.approx(low)     # nothing extra was booked
