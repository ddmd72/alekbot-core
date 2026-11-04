"""MediaEngine + the real media child + real ffmpeg (S5 design §2.4, card A): the recording reaches ffmpeg only through
the tunnel, fed by an in-test peer that plays the server route and the indexer (OPEN -> DATA from the offset until
CLOSE or EOF, only the newest open stream fed, END at the end). Media are made by ffmpeg at test time, a few seconds
each. Slow: every run starts a Python child and ffmpeg."""
import asyncio
import os
import subprocess
import sys

import pytest

from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.media_engine import MediaEngine
from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_tunnel import (
    DATA_MAX, END_EOF, MEDIA_CEILING, Frame, FrameType, TunnelProbe, TunnelStart, TunnelWindow, TunnelWindows,
    tunnel_budget,
)
from tests.unit.adapters.drive_media_fixtures import HAVE_FFMPEG, make_audio_mp4, make_cueless_webm

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed"),
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"),
                       reason="children run as the invoking user: needs a non-root user"),
]

_SECONDS = 6
_MP4_CONTAINER = "mov,mp4,m4a,3gp,3g2,mj2"


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   stdout=subprocess.DEVNULL)


def _mp3(path):
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=330:duration={_SECONDS}", "-c:a", "libmp3lame", "-b:a", "32k",
            str(path))
    return path.read_bytes()


def _silent_film(path):
    """A video with no audio stream at all."""
    _ffmpeg("-f", "lavfi", "-i", f"color=c=green:s=16x16:r=2:d={_SECONDS}", "-c:v", "libx264", "-preset",
            "ultrafast", str(path))
    return path.read_bytes()


def _duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         check=True, stdout=subprocess.PIPE).stdout
    return float(out.strip())


class Peer:
    """The route + indexer side of the tunnel over one recording held in memory."""

    def __init__(self, link, data: bytes):
        self._link = link
        self._data = data
        self._order = []                 # open streams, oldest first: only the newest is fed
        self._turn = asyncio.Condition()
        self._feeders = {}
        self.opens = []

    async def serve(self):
        try:
            while (frame := await self._link.receive()) is not None:
                async with self._turn:
                    if frame.kind is FrameType.OPEN:
                        self.opens.append(frame.value)
                        self._order.append(frame.id)
                        self._feeders[frame.id] = asyncio.ensure_future(self._feed(frame.id, frame.value))
                    else:
                        self._order.remove(frame.id)
                        self._feeders.pop(frame.id).cancel()
                    self._turn.notify_all()
        finally:
            for task in self._feeders.values():
                task.cancel()

    async def _feed(self, stream_id, offset):
        position = offset
        while position < len(self._data):
            async with self._turn:
                await self._turn.wait_for(lambda: self._order and self._order[-1] == stream_id)
            chunk = self._data[position:position + DATA_MAX]
            await self._link.send(Frame(FrameType.DATA, stream_id, position, chunk))
            position += len(chunk)
        await self._link.send(Frame(FrameType.END, stream_id, END_EOF))


async def _run(tmp_path, data: bytes, op: SandboxOp, **fields):
    workdir = tmp_path / f"work-{op.value}"
    workdir.mkdir()
    out_root = tmp_path / "out"
    out_root.mkdir(mode=0o700, exist_ok=True)
    start = TunnelStart(op=op, source_bytes=len(data), budget_bytes=tunnel_budget(len(data)), wall_s=60.0, **fields)
    engine = MediaEngine(ChildLauncher(nproc=4000, nofile=256), op=op, output_root=str(out_root))
    run = await engine.start(str(workdir), MEDIA_CEILING, start)
    peer = Peer(run.link, data)
    _, done = await asyncio.wait_for(asyncio.gather(peer.serve(), run.wait()), 90)
    return done, peer


def _windows_fields(container, duration_known=True):
    return dict(container=container, duration_known=duration_known,
                windows=(TunnelWindow(index=0, start_s=0.0, duration_s=4.0),
                         TunnelWindow(index=1, start_s=3.0, duration_s=1800.0)) if duration_known
                else (TunnelWindow(index=0, start_s=0.0, duration_s=1800.0),))


async def test_a_moov_at_end_mp4_is_probed_and_cut_through_the_tunnel(tmp_path):
    data = make_audio_mp4(tmp_path / "Community garden meeting (Alex, Kim, Jordan).mp4", _SECONDS)
    assert data.find(b"moov") > data.find(b"mdat")              # ffmpeg's default: the index after the media

    probed, peer = await _run(tmp_path, data, SandboxOp.MEDIA_PROBE)
    assert probed.result.status is SandboxStatus.OK, probed.result
    media = probed.result.media
    assert isinstance(media, TunnelProbe) and media.has_audio and media.container == _MP4_CONTAINER
    assert media.duration_known and abs(media.duration_s - _SECONDS) < 0.1
    assert probed.opens >= 2 and any(offset > 0 for offset in peer.opens)   # a seek to the moov
    assert probed.bytes_in <= tunnel_budget(len(data))

    cut, _ = await _run(tmp_path, data, SandboxOp.MEDIA_AUDIO_WINDOWS, **_windows_fields(media.container))
    windows = cut.result.media
    assert cut.result.status is SandboxStatus.OK and isinstance(windows, TunnelWindows), cut.result
    assert windows.status is SandboxStatus.OK and [w.index for w in windows.windows] == [0, 1]
    assert windows.eof and abs(windows.duration_s - _SECONDS) < 0.5
    for (index, path), spec in zip(cut.windows, windows.windows):
        assert os.path.dirname(path) == cut.output_dir
        assert abs(_duration(path) - spec.duration_s) < 0.1


async def test_a_cueless_webm_is_read_in_one_pass(tmp_path):
    data = make_cueless_webm(_SECONDS)
    probed, _ = await _run(tmp_path, data, SandboxOp.MEDIA_PROBE)
    media = probed.result.media
    assert probed.result.status is SandboxStatus.OK and isinstance(media, TunnelProbe), probed.result
    assert media.has_audio and not media.duration_known and media.duration_s == 0

    cut, _ = await _run(tmp_path, data, SandboxOp.MEDIA_AUDIO_WINDOWS,
                        **_windows_fields(media.container, duration_known=False))
    windows = cut.result.media
    assert cut.result.status is SandboxStatus.OK and isinstance(windows, TunnelWindows), cut.result
    assert [w.index for w in windows.windows] == [0] and windows.eof
    assert abs(windows.duration_s - _SECONDS) < 0.5 and not windows.reached_bound
    assert abs(_duration(cut.windows[0][1]) - _SECONDS) < 0.5


async def test_an_mp3_voice_memo_is_probed_and_cut(tmp_path):
    data = _mp3(tmp_path / "Coimbra weekend voice memo.mp3")
    probed, _ = await _run(tmp_path, data, SandboxOp.MEDIA_PROBE)
    media = probed.result.media
    assert probed.result.status is SandboxStatus.OK and media.container == "mp3" and media.has_audio, probed.result

    cut, _ = await _run(tmp_path, data, SandboxOp.MEDIA_AUDIO_WINDOWS,
                        **_windows_fields(media.container))
    windows = cut.result.media
    assert cut.result.status is SandboxStatus.OK and [w.index for w in windows.windows] == [0, 1], cut.result
    assert windows.eof


async def test_a_video_without_audio_is_probed_as_such(tmp_path):
    data = _silent_film(tmp_path / "Community garden walkthrough.mp4")
    probed, _ = await _run(tmp_path, data, SandboxOp.MEDIA_PROBE)
    media = probed.result.media
    assert probed.result.status is SandboxStatus.OK and isinstance(media, TunnelProbe), probed.result
    assert media.has_audio is False and media.container == _MP4_CONTAINER
