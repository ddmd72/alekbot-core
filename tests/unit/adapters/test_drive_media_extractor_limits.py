"""SubprocessMediaExtractor: a request the child could not read whole is refused before any process starts."""
import contextlib

import pytest

from src.adapters.drive_parser import media_launch as launch
from src.adapters.drive_parser.media_extractor import SubprocessMediaExtractor
from src.domain.drive_media_source import MediaSource
from src.domain.drive_transcript import MediaUnreadable, WindowSpec

WEBM = "matroska,webm"


def _extractor(tmp_path, url: str):
    @contextlib.asynccontextmanager
    async def opener(source, wall_s):
        yield url

    root = tmp_path / "work"
    root.mkdir()
    return SubprocessMediaExtractor(child_module="tests.unit.adapters.drive_media_child_stub", temp_root=str(root),
                                    ca_file="/x/ca.pem", child_url=opener), root


async def test_a_request_over_the_child_stdin_limit_is_unreadable_and_spawns_nothing(tmp_path, monkeypatch):
    spawned = []

    async def spawn(*args, **kwargs):
        spawned.append(args)
        return 0, False

    monkeypatch.setattr(launch, "spawn", spawn)
    extractor, root = _extractor(tmp_path, "https://media.example.invalid/" + "Hamburg-talk-" * launch.MAX_REQUEST_BYTES)
    with pytest.raises(MediaUnreadable, match="request too large"):
        await extractor.probe(MediaSource(url="https://media.example.invalid/x"))
    with pytest.raises(MediaUnreadable, match="request too large"):
        await extractor.extract_windows(MediaSource(url="https://media.example.invalid/x"), [WindowSpec(0, 0, 10)],
                                        str(tmp_path / "out"), wall_s=30, container=WEBM)
    assert spawned == [] and list(root.iterdir()) == []                 # no child, and the work directory is gone
