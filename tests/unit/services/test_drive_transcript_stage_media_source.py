"""The transcript stage hands the drive's `MediaSource` to the media port unchanged (GOOGLE_DRIVE_RFC G2, E37 moved,
design §1.6): headers included and no stage-level refusal (the port keeps every header out of the sandbox); a source the
drive cannot give is still the file's charged attempt. Synthetic data, technical ids."""
from types import SimpleNamespace
from typing import List, Sequence, Tuple

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_media_source import MediaSource
from src.domain.drive_transcript import MediaProbeOutcome, MediaWindowsOutcome, WindowSpec
from src.domain.user_drive import DriveItemNotFoundError
from src.services.drive_transcript_stage import DriveTranscriptStage

from .drive_transcript_fakes import (
    HANDLE, USER, Clock, FakeBatch, FakeDrive, FakeHost, FakeMedia, FakeRunner, Recording, record,
)

HEADERS = {"Authorization": "Bearer tok-a1"}
STEP_S = 600.0


class _SourceDrive(FakeDrive):
    """Records every source it hands out."""

    def __init__(self) -> None:
        super().__init__()
        self.given: List[MediaSource] = []

    async def media_source(self, handle, item_id: str) -> MediaSource:
        source = await super().media_source(handle, item_id)
        self.given.append(source)
        return source


class _SourceMedia(FakeMedia):
    """Records the source of every probe and extraction."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock)
        self.sources: List[Tuple[str, MediaSource]] = []

    async def probe(self, source: MediaSource) -> MediaProbeOutcome:
        self.sources.append(("probe", source))
        return await super().probe(source)

    async def extract_windows(self, source: MediaSource, windows: Sequence[WindowSpec], out_dir: str, *, wall_s: float,
                              container: str, duration_known: bool = True) -> MediaWindowsOutcome:
        self.sources.append(("extract", source))
        return await super().extract_windows(source, windows, out_dir, wall_s=wall_s, container=container,
                                             duration_known=duration_known)


def _env(tmp_path):
    clock = Clock()
    repo = InMemoryDriveIndexRepository()
    media, drive = _SourceMedia(clock), _SourceDrive()
    host = FakeHost(repo, clock)
    stage = DriveTranscriptStage(repo, drive, media, FakeBatch(clock), FakeRunner(), clock=clock,
                                 monotonic=clock.monotonic, monthly_cap_usd=20.0, temp_root=str(tmp_path))
    return SimpleNamespace(clock=clock, repo=repo, media=media, drive=drive, host=host, stage=stage)


async def _step(env):
    return await env.stage.extract_step(HANDLE, env.host, stop_at=env.clock.mono + STEP_S)


@pytest.mark.parametrize("headers", [HEADERS, {}])
async def test_the_drives_source_reaches_the_media_port_unchanged(tmp_path, headers):
    env = _env(tmp_path)
    env.media.recordings["a"] = Recording(600)
    env.drive.headers = dict(headers)
    await env.repo.upsert_records([record("a", transcript_state="ready")])
    summary = await _step(env)
    assert [op for op, _ in env.media.sources] == ["probe", "extract"]
    assert all(source is env.drive.given[0] for _, source in env.media.sources)
    assert dict(env.drive.given[0].headers) == headers
    assert summary.get("transcript_attempts_charged", 0) == 0
    assert summary["transcript_started"] == 1


async def test_a_header_carrying_source_is_not_refused_by_the_stage(tmp_path):
    env = _env(tmp_path)
    env.media.recordings["a"] = Recording(600)
    env.drive.headers = dict(HEADERS)
    await env.repo.upsert_records([record("a", transcript_state="ready")])
    await _step(env)
    job = env.repo.transcript_jobs[(USER, "a")]
    assert job.windows and env.media.runs


async def test_a_source_the_drive_cannot_give_is_still_charged(tmp_path):
    env = _env(tmp_path)
    env.media.recordings["a"] = Recording(600)
    env.drive.raise_for["a"] = DriveItemNotFoundError("a")
    await env.repo.upsert_records([record("a", transcript_state="ready")])
    summary = await _step(env)
    assert summary["transcript_attempts_charged"] == 1
    assert env.media.sources == []
    assert (await env.repo.get_records(HANDLE.scope, ["a"]))["a"].transcript_attempts == 1
