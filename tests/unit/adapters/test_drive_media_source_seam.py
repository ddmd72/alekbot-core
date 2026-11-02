"""The extractor's child-URL seam (GOOGLE_DRIVE_RFC G2 core half, E37, design §1.6): the default opener gives the child
a header-free source's url and refuses a source that needs headers before any child runs; an injected opener is entered
once per child run, under the one slot, and left on a normal exit, a wall kill and a cancellation."""
import asyncio
import contextlib
import logging
from typing import Any, AsyncIterator, Dict, List

import pytest

from src.adapters.drive_parser import media_launch as launch
from src.adapters.drive_parser.media_extractor import SubprocessMediaExtractor, header_free_url
from src.domain.drive_media_source import MediaSource
from src.domain.drive_transcript import MediaStatus, MediaUnreadable, WindowSpec

WEBM = "matroska,webm"
STUB = "tests.unit.adapters.drive_media_child_stub"
SECRET_URL = "https://media.example.invalid/fid-1?sig=SECRET-SIG-123"
SECRET_HEADER = "Bearer SECRET-TOKEN-456"
WALL_S = 30.0


def _stub(tmp_path, **kwargs) -> SubprocessMediaExtractor:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    return SubprocessMediaExtractor(child_module=STUB, temp_root=str(root), ca_file="/x/ca.pem", **kwargs)


def _record_spawns(monkeypatch) -> List[Dict[str, Any]]:
    requests: List[Dict[str, Any]] = []
    real = launch.spawn

    async def record(request, *args, **kwargs):
        requests.append(dict(request))
        return await real(request, *args, **kwargs)

    monkeypatch.setattr(launch, "spawn", record)
    return requests


class _Opener:
    """An injected opener that records every enter and exit, with the run's wall."""

    def __init__(self, url: str = "ok") -> None:
        self.url = url
        self.events: List[str] = []
        self.walls: List[float] = []

    @contextlib.asynccontextmanager
    async def __call__(self, source: MediaSource, wall_s: float) -> AsyncIterator[str]:
        self.events.append("enter")
        self.walls.append(wall_s)
        try:
            yield self.url
        finally:
            self.events.append("exit")


# -- the default opener ---------------------------------------------------------------------------------------------

async def test_a_header_free_source_reaches_the_child_as_its_url(tmp_path, monkeypatch):
    requests = _record_spawns(monkeypatch)
    outcome = await _stub(tmp_path).extract_windows(MediaSource(url="ok"), [WindowSpec(0, 0, 10)], str(tmp_path / "o"),
                                                    wall_s=WALL_S, container=WEBM)
    assert outcome.status is MediaStatus.OK and len(outcome.windows) == 1
    assert [r["url"] for r in requests] == ["ok"]


async def test_the_default_opener_yields_the_url_of_a_header_free_source():
    async with header_free_url(MediaSource(url=SECRET_URL), WALL_S) as url:
        assert url == SECRET_URL


@pytest.mark.parametrize("call", ["probe", "extract_windows"])
async def test_a_source_that_needs_headers_is_refused_before_any_child(tmp_path, monkeypatch, caplog, call):
    requests = _record_spawns(monkeypatch)
    source = MediaSource(url=SECRET_URL, headers={"Authorization": SECRET_HEADER})
    extractor = _stub(tmp_path)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(MediaUnreadable):
        if call == "probe":
            await extractor.probe(source)
        else:
            await extractor.extract_windows(source, [WindowSpec(0, 0, 10)], str(tmp_path / "o"), wall_s=WALL_S,
                                            container=WEBM)
    assert requests == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert "SECRET" not in caplog.text
    assert list((tmp_path / "work").iterdir()) == []


# -- an injected opener ---------------------------------------------------------------------------------------------

async def test_an_injected_opener_is_entered_once_per_run_and_left_after_a_normal_exit(tmp_path, monkeypatch):
    requests = _record_spawns(monkeypatch)
    opener = _Opener("ok")
    extractor = _stub(tmp_path, child_url=opener)
    outcome = await extractor.extract_windows(MediaSource(url=SECRET_URL, headers={"Authorization": SECRET_HEADER}),
                                              [WindowSpec(0, 0, 10)], str(tmp_path / "o"), wall_s=WALL_S,
                                              container=WEBM)
    assert outcome.status is MediaStatus.OK
    assert opener.events == ["enter", "exit"] and opener.walls == [WALL_S]
    assert [r["url"] for r in requests] == ["ok"]
    assert all(SECRET_HEADER not in str(v) for r in requests for v in r.values())


async def test_runs_take_the_slot_before_the_opener(tmp_path, monkeypatch):
    opener = _Opener("ok")
    running: List[int] = []
    peak: List[int] = []

    async def spawn(request, workdir, errors, **kwargs):
        running.append(1)
        peak.append(len(running))
        assert opener.events[-1] == "enter"             # the opener is open while the child runs
        await asyncio.sleep(0.01)
        running.pop()
        return 0, False

    monkeypatch.setattr(launch, "spawn", spawn)
    monkeypatch.setattr(launch, "load_result", lambda out: {"status": "error", "detail": "stub"})
    extractor = _stub(tmp_path, child_url=opener)
    results = await asyncio.gather(*(extractor.probe(MediaSource(url="ok")) for _ in range(3)), return_exceptions=True)
    assert all(isinstance(r, MediaUnreadable) for r in results)
    assert opener.events == ["enter", "exit"] * 3        # never two open at once: entered under the one slot
    assert max(peak) == 1


async def test_an_injected_opener_is_left_after_a_wall_kill(tmp_path, monkeypatch):
    async def spawn(request, workdir, errors, **kwargs):
        return -9, True

    monkeypatch.setattr(launch, "spawn", spawn)
    opener = _Opener("ok")
    outcome = await _stub(tmp_path, child_url=opener).extract_windows(
        MediaSource(url="ok"), [WindowSpec(0, 0, 10)], str(tmp_path / "o"), wall_s=WALL_S, container=WEBM)
    assert outcome.status is MediaStatus.TIMEOUT
    assert opener.events == ["enter", "exit"]
    assert list((tmp_path / "work").iterdir()) == []


async def test_an_injected_opener_is_left_when_the_run_is_cancelled(tmp_path, monkeypatch):
    started = asyncio.Event()

    async def spawn(request, workdir, errors, **kwargs):
        started.set()
        await asyncio.Event().wait()
        return 0, False

    monkeypatch.setattr(launch, "spawn", spawn)
    opener = _Opener("ok")
    task = asyncio.create_task(_stub(tmp_path, child_url=opener).probe(MediaSource(url="ok")))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert opener.events == ["enter"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert opener.events == ["enter", "exit"]
    assert list((tmp_path / "work").iterdir()) == []
