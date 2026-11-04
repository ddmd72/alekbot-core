"""The media runner's behaviour on real recordings, through the sandbox: `MediaEngine` + the real media child + real
ffprobe / ffmpeg, the recording fed over the tunnel by card A's in-test peer (S5 design §2.4). These are the cases the
in-process extractor's tests covered with real ffmpeg (deleted with it in card F) whose behaviour lives on in
`drive_parser/media.py`: measured windows, the pass and its clock, the CPU / file-size cuts that keep a prefix, a limit
before the first window, overstated durations, corrupt packets and clusters re-run, hostile files, and the
`TRANSCRIBE_MAX_SECONDS` bound (`too_long`). Media are made by ffmpeg at test time; long ones by stream-copying a short
segment in a loop, so only seconds of audio are ever encoded. Slow (each run starts a Python child and ffmpeg);
the ones whose outcome is set by a CPU limit against decode speed are `timing` (T-R33).

The drop / reconnect cases are about the transport and run end to end in
`tests/integration/sandbox/test_media_tunnel_contract.py`."""
import asyncio
import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.media_engine import MediaEngine
from src.domain.drive_index_params import TRANSCRIBE_MAX_SECONDS
from src.domain.drive_transcript_windows import plan_windows
from src.domain.sandbox_contract import SandboxLimits, SandboxOp, SandboxStatus
from src.domain.sandbox_tunnel import MEDIA_CEILING, TunnelProbe, TunnelStart, TunnelWindow, TunnelWindows, tunnel_budget
from tests.unit.adapters.drive_media_fixtures import (
    HAVE_FFMPEG, make_audio_mp4, make_corrupt_cueless_mkv, make_corrupt_mp4, make_cueless_webm, make_overstated_mp3,
    make_overstated_mp4, make_stepped_tone_webm, tone_hz_at,
)
from tests.unit.adapters.sandbox_engine.test_media_engine_ffmpeg import Peer

pytestmark = [
    pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed"),
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"),
                       reason="children run as the invoking user: needs a non-root user"),
]

WEBM = "matroska,webm"
MP4 = "mov,mp4,m4a,3gp,3g2,mj2"
_runs = itertools.count()


def _ffmpeg(*args: str, capture: bool = False) -> bytes:
    done = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL)
    return done.stdout


def _duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         check=True, stdout=subprocess.PIPE).stdout
    return float(out.strip())


def _looped_cueless_webm(tmp_path: Path, total_s: int, *, bitrate: str = "6k") -> bytes:
    """A pipe-written (no Duration, no Cues) Opus webm of about `total_s` seconds: a 60 s tone encoded once, then
    stream-copied in a loop."""
    segment = tmp_path / f"segment-{next(_runs)}.webm"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=60", "-ac", "1", "-c:a", "libopus", "-b:a", bitrate,
            str(segment))
    loops = -(-total_s // 60) - 1
    return _ffmpeg("-stream_loop", str(loops), "-i", str(segment), "-c", "copy", "-f", "webm", "pipe:1", capture=True)


def _looped_m4a(tmp_path: Path, total_s: int) -> bytes:
    """An AAC mp4 with a stated duration of about `total_s` seconds, made the same way (8 kbit/s, mono)."""
    segment = tmp_path / f"segment-{next(_runs)}.m4a"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=60:sample_rate=8000", "-ac", "1", "-c:a", "aac",
            "-b:a", "8k", str(segment))
    out = tmp_path / f"Community garden archive {next(_runs)}.m4a"
    _ffmpeg("-stream_loop", str(-(-total_s // 60) - 1), "-i", str(segment), "-c", "copy", str(out))
    return out.read_bytes()


async def _run(tmp_path: Path, data: bytes, op: SandboxOp, *, cpu_s: float = MEDIA_CEILING.cpu_s,
               file_bytes: int = MEDIA_CEILING.file_bytes, wall_s: float = 120.0, **fields):
    workdir = tmp_path / f"work-{next(_runs)}"
    workdir.mkdir()
    out_root = tmp_path / "out"
    out_root.mkdir(mode=0o700, exist_ok=True)
    limits = SandboxLimits(wall_s, MEDIA_CEILING.memory_bytes, cpu_s, file_bytes)
    start = TunnelStart(op=op, source_bytes=len(data), budget_bytes=tunnel_budget(len(data)), wall_s=wall_s, **fields)
    engine = MediaEngine(ChildLauncher(nproc=4000, nofile=256), op=op, output_root=str(out_root))
    run = await engine.start(str(workdir), limits, start)
    peer = Peer(run.link, data)
    _, done = await asyncio.wait_for(asyncio.gather(peer.serve(), run.wait()), wall_s + 60)
    return done, peer


async def _probe(tmp_path: Path, data: bytes) -> TunnelProbe:
    done, _ = await _run(tmp_path, data, SandboxOp.MEDIA_PROBE)
    assert done.result.status is SandboxStatus.OK and isinstance(done.result.media, TunnelProbe), done.result
    return done.result.media


async def _windows(tmp_path: Path, data: bytes, plan, container: str, *, duration_known: bool = True, **limits):
    windows = tuple(TunnelWindow(index=w.index, start_s=float(w.start_s), duration_s=float(w.duration_s))
                    for w in plan)
    return await _run(tmp_path, data, SandboxOp.MEDIA_AUDIO_WINDOWS, container=container,
                      duration_known=duration_known, windows=windows, **limits)


def _media(done) -> TunnelWindows:
    assert done.result.status is SandboxStatus.OK and isinstance(done.result.media, TunnelWindows), done.result
    return done.result.media


def _w(index: int, start_s: float, duration_s: float) -> TunnelWindow:
    return TunnelWindow(index=index, start_s=start_s, duration_s=duration_s)


# ---- known duration: measured windows -----------------------------------------------------------------------------

@pytest.mark.slow
async def test_known_duration_windows_are_cut_at_their_measured_length(tmp_path):
    """Plan-shaped windows of a 100 s recording: the last is read at a full window's length (M2) and a header that is
    right ends it where the plan does, so no eof; each window's length is measured, not planned."""
    data = make_audio_mp4(tmp_path / "Community garden meeting (Alex, Kim, Jordan).mp4", 100)
    probe = await _probe(tmp_path, data)
    assert probe.duration_known and abs(probe.duration_s - 100.0) < 1.0
    done, peer = await _windows(tmp_path, data, [_w(0, 0.0, 40.0), _w(1, 30.0, 70.0)], probe.container)
    media = _media(done)
    assert media.status is SandboxStatus.OK and not media.eof
    assert [(w.index, w.start_s) for w in media.windows] == [(0, 0.0), (1, 30.0)]
    assert [round(w.duration_s) for w in media.windows] == [40, 70]
    for _, path in done.windows:
        assert Path(path).read_bytes()[:4] == b"OggS"
    assert any(offset > 0 for offset in peer.opens)                  # input-side -ss: ranges, not a full read


# ---- the pass: one read, windows cut from it, the clock kept --------------------------------------------------------

@pytest.mark.slow
async def test_a_pass_cuts_overlapping_windows_whose_content_matches_the_clock(tmp_path):
    data = make_stepped_tone_webm(100)                              # pitch = 300 Hz + 100 Hz per started 10 s
    plan = plan_windows(100.0, window_s=40, overlap_s=10)
    assert [(w.index, w.start_s) for w in plan] == [(0, 0.0), (1, 30.0), (2, 60.0)]
    done, _ = await _windows(tmp_path, data, plan, WEBM, duration_known=False)
    media = _media(done)
    assert media.status is SandboxStatus.OK and not media.eof and [w.index for w in media.windows] == [0, 1, 2]
    for spec, (_, path) in zip(media.windows, done.windows):
        for abs_t in (5.0 + 10 * k for k in range(10)):              # the middle of every 10 s step
            rel = abs_t - spec.start_s
            if 0 <= rel <= spec.duration_s - 0.6:
                assert tone_hz_at(path, rel) == pytest.approx(300 + 100 * int(abs_t // 10), abs=25), (spec, abs_t)


@pytest.mark.slow
async def test_a_resumed_pass_starts_at_the_slice_and_keeps_the_clock(tmp_path):
    data = make_stepped_tone_webm(100)
    plan = plan_windows(100.0, window_s=40, overlap_s=10)[1:]        # the first window is done
    done, _ = await _windows(tmp_path, data, plan, WEBM, duration_known=False)
    media = _media(done)
    assert [(w.index, w.start_s) for w in media.windows] == [(1, 30.0), (2, 60.0)]
    (_, path1), (_, path2) = done.windows
    assert tone_hz_at(path1, 5.0) == pytest.approx(300 + 100 * 3, abs=25)      # abs 35 s
    assert tone_hz_at(path2, 5.0) == pytest.approx(300 + 100 * 6, abs=25)      # abs 65 s
    assert not media.eof and abs(media.windows[1].duration_s - 40.0) < 0.5


# ---- limits: a cut pass keeps its prefix; a limit before the first window is the file's error ---------------------

@pytest.mark.timing   # the outcome is set by a 3 CPU-s RLIMIT_CPU against decode speed (T-R33)
@pytest.mark.slow
async def test_a_pass_cut_by_the_cpu_limit_returns_the_windows_inside_the_prefix(tmp_path):
    data = _looped_cueless_webm(tmp_path, 1_500, bitrate="24k")
    plan = [_w(0, 0.0, 60.0), _w(1, 50.0, 700.0), _w(2, 740.0, 760.0)]
    media = _media((await _windows(tmp_path, data, plan, WEBM, duration_known=False, cpu_s=3))[0])
    assert media.status is SandboxStatus.TIMEOUT and not media.eof
    assert 0 < len(media.windows) < 3 and media.windows[0].index == 0


@pytest.mark.slow
async def test_a_pass_cut_by_the_file_size_limit_returns_the_windows_inside_the_prefix(tmp_path):
    data = _looped_cueless_webm(tmp_path, 1_500, bitrate="24k")
    plan = [_w(0, 0.0, 30.0), _w(1, 20.0, 700.0), _w(2, 710.0, 790.0)]
    done, _ = await _windows(tmp_path, data, plan, WEBM, duration_known=False, file_bytes=200_000)
    media = _media(done)
    assert media.status is SandboxStatus.TIMEOUT and not media.eof
    assert 0 < len(media.windows) < 3 and media.windows[0].index == 0
    assert all(Path(p).read_bytes()[:4] == b"OggS" for _, p in done.windows)


@pytest.mark.timing   # the outcome is set by a 3 CPU-s RLIMIT_CPU against decode speed (T-R33)
@pytest.mark.slow
async def test_a_limit_before_the_first_window_is_an_error_not_a_loop(tmp_path):
    """N2: a CPU cut that finishes no window would repeat forever as a TIMEOUT; it is the file's error."""
    data = _looped_cueless_webm(tmp_path, 3_600, bitrate="24k")
    done, _ = await _windows(tmp_path, data, plan_windows(14_400)[:3], WEBM, duration_known=False, cpu_s=3)
    assert done.result.status is SandboxStatus.ERROR and done.windows == ()


# ---- durations that overstate the audio (T-R16) -------------------------------------------------------------------

@pytest.mark.slow
async def test_video_longer_than_the_audio_ends_at_the_audio(tmp_path):
    data = make_overstated_mp4(tmp_path / "Community garden walkthrough.mp4", video_s=100, audio_s=60)
    probe = await _probe(tmp_path, data)
    assert abs(probe.duration_s - 100.0) < 1.0
    media = _media((await _windows(tmp_path, data, [_w(0, 0.0, 40.0), _w(1, 30.0, 40.0), _w(2, 60.0, 40.0)],
                                   probe.container))[0])
    assert media.status is SandboxStatus.OK and media.eof and abs(media.duration_s - 60.0) < 1.0
    assert [w.index for w in media.windows] == [0, 1]                 # window 2 was never run
    assert abs(media.windows[1].duration_s - 30.0) < 1.0


@pytest.mark.slow
async def test_an_mp3_whose_header_overstates_the_duration_ends_at_the_audio(tmp_path):
    data = make_overstated_mp3(tmp_path / "Coimbra weekend voice memo.mp3", 120)
    probe = await _probe(tmp_path, data)
    if probe.duration_s < 122:
        pytest.skip("this ffmpeg estimates the duration exactly")
    media = _media((await _windows(tmp_path, data, [_w(0, 0.0, probe.duration_s)], probe.container))[0])
    assert media.status is SandboxStatus.OK and media.eof and abs(media.duration_s - 120.0) < 2.0
    assert len(media.windows) == 1


# ---- corrupt packets and clusters: re-run without -xerror (T-R16, N7) ----------------------------------------------

@pytest.mark.slow
async def test_one_corrupt_packet_does_not_fail_the_file(tmp_path):
    data = make_corrupt_mp4(tmp_path / "Community garden meeting minutes.mp4", 120)
    probe = await _probe(tmp_path, data)
    media = _media((await _windows(tmp_path, data, [_w(0, 0.0, 120.0)], probe.container))[0])
    assert media.status is SandboxStatus.OK and len(media.windows) == 1 and not media.eof


@pytest.mark.timing   # each run must stay under a 3 CPU-s RLIMIT_CPU (T-R33)
@pytest.mark.slow
async def test_a_corrupt_packet_late_in_a_long_file_is_re_run_after_earlier_windows_spent_cpu(tmp_path):
    """N7: the re-run decision judges the failed run's own CPU, not the CPU every earlier window spent."""
    data = make_corrupt_mp4(tmp_path / "Community garden meeting (long).mp4", 900, at=0.9)
    plan = plan_windows(900.0, window_s=60, overlap_s=5)
    probe = await _probe(tmp_path, data)
    media = _media((await _windows(tmp_path, data, plan, probe.container, cpu_s=3, wall_s=170.0))[0])
    assert media.status is SandboxStatus.OK and not media.eof
    assert [w.index for w in media.windows] == [w.index for w in plan]


@pytest.mark.slow
async def test_a_corrupt_cluster_in_a_pass_is_re_run_and_yields_its_windows(tmp_path):
    data = make_corrupt_cueless_mkv(150, at=0.5)
    probe = await _probe(tmp_path, data)
    assert not probe.duration_known and probe.container == WEBM
    plan = plan_windows(150.0, window_s=60, overlap_s=10)
    media = _media((await _windows(tmp_path, data, plan, probe.container, duration_known=False))[0])
    assert media.status is SandboxStatus.OK and [w.index for w in media.windows] == [w.index for w in plan]


# ---- the bound: TRANSCRIBE_MAX_SECONDS (`too_long`) ------------------------------------------------------------------

@pytest.mark.slow
async def test_a_pass_that_ends_at_the_bound_with_the_file_going_on_reaches_the_bound(tmp_path):
    """The `too_long` stub's input for a recording without a duration: the last window of a pass ends at
    TRANSCRIBE_MAX_SECONDS and the audio goes on, so the run says `reached_bound` (and no eof). The window is the
    plan's last minute, so only a minute is decoded."""
    data = _looped_cueless_webm(tmp_path, TRANSCRIBE_MAX_SECONDS + 180)
    last = plan_windows(float(TRANSCRIBE_MAX_SECONDS))[-1]
    window = _w(last.index, TRANSCRIBE_MAX_SECONDS - 60.0, 60.0)
    done, _ = await _windows(tmp_path, data, [window], WEBM, duration_known=False)
    media = _media(done)
    assert media.status is SandboxStatus.OK and media.reached_bound and not media.eof
    (spec,) = media.windows
    assert spec.index == last.index and abs(spec.duration_s - 60.0) < 0.5


@pytest.mark.slow
async def test_a_recording_over_four_hours_probes_over_the_bound(tmp_path):
    """The `too_long` input for a recording with a duration: the probe states it, and the stage refuses on it."""
    probe = await _probe(tmp_path, _looped_m4a(tmp_path, TRANSCRIBE_MAX_SECONDS + 300))
    assert probe.duration_known and probe.has_audio and probe.duration_s > TRANSCRIBE_MAX_SECONDS


@pytest.mark.timing   # one full plan window decoded under the media ceiling's CPU limit (T-R33)
@pytest.mark.slow
async def test_one_full_window_deep_in_four_hours_stays_under_the_limits(tmp_path):
    """A full TRANSCRIBE_WINDOW_S window from deep inside a four-hour recording comes out as one valid Opus window
    under the media ceiling (CPU 150 s, file 64 MiB, address space 1 GiB)."""
    data = _looped_m4a(tmp_path, TRANSCRIBE_MAX_SECONDS)
    done, _ = await _windows(tmp_path, data, [_w(7, 7_000.0, 1_800.0)], MP4, wall_s=MEDIA_CEILING.wall_s)
    media = _media(done)
    assert media.status is SandboxStatus.OK and not media.eof
    (spec,) = media.windows
    assert spec.index == 7 and abs(spec.duration_s - 1_800.0) < 1.0
    assert abs(_duration(done.windows[0][1]) - 1_800.0) < 1.0


# ---- hostile files end as a defined outcome ----------------------------------------------------------------------

def _hostile(tmp_path: Path, name: str) -> bytes:
    if name == "concat":
        return b"ffconcat version 1.0\nfile 'file:///etc/passwd'\nfile 'https://other.invalid/y.mp3'\n"
    if name == "truncated":                                          # the moov atom sits at the end of the full file
        return make_audio_mp4(tmp_path / "Community garden meeting.mp4", 20)[:3000]
    return bytes(range(256)) * 40                                    # garbage


@pytest.mark.slow
@pytest.mark.parametrize("name", ["concat", "truncated", "garbage"])
async def test_a_hostile_file_ends_as_the_files_error_with_no_window(tmp_path, name):
    data = _hostile(tmp_path, name)
    probed, _ = await _run(tmp_path, data, SandboxOp.MEDIA_PROBE)
    assert probed.result.status is SandboxStatus.ERROR
    cut, _ = await _windows(tmp_path, data, [_w(0, 0.0, 10.0)], MP4)
    assert cut.result.status is SandboxStatus.ERROR and cut.windows == ()


@pytest.mark.slow
async def test_a_demuxer_outside_the_allowlist_cannot_be_pinned(tmp_path):
    playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n"
    done, _ = await _windows(tmp_path, playlist, [_w(0, 0.0, 10.0)], "hls")
    assert done.result.status is not SandboxStatus.OK and done.windows == ()


@pytest.mark.slow
async def test_a_cueless_recording_is_read_in_one_pass_to_its_end(tmp_path):
    """The pass of the standard plan over a 100 s browser recording reads the file once and reports its end (G35)."""
    data = make_cueless_webm(100)
    probe = await _probe(tmp_path, data)
    assert not probe.duration_known and probe.duration_s == 0.0
    done, _ = await _windows(tmp_path, data, plan_windows(14_400)[:3], probe.container, duration_known=False)
    media = _media(done)
    assert media.status is SandboxStatus.OK and media.eof and not media.reached_bound
    assert abs(media.duration_s - 100.0) < 1.0
    (spec,) = media.windows
    assert spec.index == 0 and spec.start_s == 0.0 and abs(spec.duration_s - 100.0) < 1.0
    assert len(done.windows) == 1                                    # pass.opus is not a window
