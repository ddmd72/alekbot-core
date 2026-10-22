"""SubprocessMediaExtractor (USER_DRIVE_INDEXING_RFC §27.4, Δ99, Δ134; plan G14, G15, G35): the launcher contract
(allowlisted env, parser user, process-group kill, collected windows) through a stub child, and the real ffprobe/ffmpeg
path against a local HTTPS server with a test CA (skipped without ffmpeg)."""
import asyncio
import json
import os
import signal
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.drive_parser import media, media_launch as launch
from src.adapters.drive_parser.media_extractor import SubprocessMediaExtractor
from src.domain.drive_transcript_windows import plan_windows
from src.domain.drive_transcript import MediaStatus, MediaUnreadable, WindowSpec
from src.ports.media_audio_port import MediaAudioPort
from tests.unit.adapters.drive_media_fixtures import (
    HAVE_FFMPEG, MediaServer, make_audio_mp4, make_cueless_webm, make_corrupt_cueless_mkv, make_corrupt_mp4, make_overstated_mp3, make_overstated_mp4, make_seekable_webm, make_stepped_tone_webm,
    tone_hz_at,
)

pytestmark = pytest.mark.datafile

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg and ffprobe")
WEBM = "matroska,webm"
STUB = "tests.unit.adapters.drive_media_child_stub"
UID, GID = os.getuid(), os.getgid()


def _stub(tmp_path, **kwargs):
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    return SubprocessMediaExtractor(child_module=STUB, temp_root=str(root), ca_file="/x/ca.pem", **kwargs)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# =============================================================================
# Launcher contract (stub child)
# =============================================================================

def test_it_is_a_media_audio_port():
    assert issubclass(SubprocessMediaExtractor, MediaAudioPort)


def test_exit_codes_mirror_the_child():
    from src.adapters.drive_parser import child
    assert (launch.EXIT_MEMORY, launch.EXIT_BAD_REQUEST) == (child.EXIT_MEMORY, child.EXIT_BAD_REQUEST)


async def test_env_is_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET_FOR_TEST", "hunter2")
    monkeypatch.setenv("PYTHONPATH", "/evil")
    captured = {}
    real = launch.asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        captured.update(kwargs["env"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(launch.asyncio, "create_subprocess_exec", record)
    out = tmp_path / "out"
    extractor = _stub(tmp_path)
    await extractor.extract_windows("env", [WindowSpec(0, 0, 10)], str(out), wall_s=30, container=WEBM)
    assert set(captured) <= {"PATH", "HOME", "LANG", "LC_ALL", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                             "MKL_NUM_THREADS", "MALLOC_ARENA_MAX", "TMPDIR"}
    assert "CLIENT_SECRET_FOR_TEST" not in captured and "PYTHONPATH" not in captured


async def test_runs_as_parser_user_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(launch.pwd, "getpwnam",
                        lambda name: SimpleNamespace(pw_name=name, pw_uid=UID, pw_gid=GID))
    monkeypatch.setattr(launch, "_effective_uid", lambda: 0)
    spawns = []
    real = launch.asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        workdir = kwargs["env"]["TMPDIR"]
        spawns.append({"kwargs": dict(kwargs), "mode": stat.S_IMODE(os.stat(workdir).st_mode),
                       "owner": (os.stat(workdir).st_uid, os.stat(workdir).st_gid)})
        for key in ("user", "group", "extra_groups"):
            kwargs.pop(key, None)
        return await real(*args, **kwargs)

    monkeypatch.setattr(launch.asyncio, "create_subprocess_exec", record)
    extractor = _stub(tmp_path, sandbox_user="driveparser")
    outcome = await extractor.extract_windows("ok", [WindowSpec(0, 0, 10)], str(tmp_path / "out"), wall_s=30, container=WEBM)
    assert outcome.status is MediaStatus.OK
    (spawn,) = spawns
    kwargs = spawn["kwargs"]
    assert (kwargs["user"], kwargs["group"], kwargs["extra_groups"]) == (UID, GID, [])
    assert kwargs["start_new_session"] is True and kwargs["env"]["HOME"] == kwargs["env"]["TMPDIR"]
    assert spawn["mode"] == 0o700 and spawn["owner"] == (UID, GID)
    assert list((tmp_path / "work").iterdir()) == []             # the private directory is removed afterwards


@pytest.mark.parametrize("setup,message", [
    (lambda m: m.setattr(launch.pwd, "getpwnam", lambda n: (_ for _ in ()).throw(KeyError(n))), "does not exist"),
    (lambda m: (m.setattr(launch.pwd, "getpwnam", lambda n: SimpleNamespace(pw_uid=0, pw_gid=0)),
                m.setattr(launch, "_effective_uid", lambda: 0)), "root"),
    (lambda m: (m.setattr(launch.pwd, "getpwnam", lambda n: SimpleNamespace(pw_uid=5, pw_gid=5)),
                m.setattr(launch, "_effective_uid", lambda: 1000)), "needs root"),
])
def test_a_bad_parser_user_fails_the_start(monkeypatch, setup, message):
    setup(monkeypatch)
    with pytest.raises(RuntimeError, match=message):
        SubprocessMediaExtractor(sandbox_user="driveparser")


@pytest.mark.timing   # asserts a wall-clock bound (< 15 s) (owner rule 2026-10-20, T-R33)
async def test_wall_kills_group(tmp_path):
    out = tmp_path / "out"
    extractor = _stub(tmp_path)
    started = time.monotonic()
    outcome = await extractor.extract_windows("sleep_group", [WindowSpec(0, 0, 10)], str(out), wall_s=3, container=WEBM)
    assert outcome.status is MediaStatus.TIMEOUT and outcome.windows == ()
    assert time.monotonic() - started < 15


@pytest.mark.slow
async def test_wall_kill_reaches_the_grandchild(tmp_path, monkeypatch):
    seen = []
    real = launch.kill_group

    def spy(pid):
        seen.append(pid)
        real(pid)

    monkeypatch.setattr(launch, "kill_group", spy)
    extractor = _stub(tmp_path)
    await extractor.extract_windows("sleep_group", [WindowSpec(0, 0, 10)], str(tmp_path / "out"), wall_s=3, container=WEBM)
    (pgid,) = seen
    await asyncio.sleep(0.3)
    with pytest.raises(ProcessLookupError):                      # nothing is left in the child's process group
        os.killpg(pgid, 0)


async def test_collected_windows_are_copied_and_the_directory_removed(tmp_path):
    out = tmp_path / "out"
    outcome = await _stub(tmp_path).extract_windows("ok", [WindowSpec(0, 0, 10)], str(out), wall_s=30, container=WEBM)
    ((spec, path),) = outcome.windows
    assert spec == WindowSpec(0, 0.0, 10.0) and path == str(out / "window_0.opus")
    assert stat.S_ISREG(os.lstat(path).st_mode) and list((tmp_path / "work").iterdir()) == []


@pytest.mark.parametrize("label", ["symlink", "fifo", "missing", "bad_entry"])
async def test_collected_window_must_be_regular_file(tmp_path, label):
    out = tmp_path / "out"
    with pytest.raises(MediaUnreadable):
        await _stub(tmp_path).extract_windows(label, [WindowSpec(0, 0, 10)], str(out), wall_s=30, container=WEBM)
    assert not list(out.glob("window_*")) and list((tmp_path / "work").iterdir()) == []


async def test_a_window_over_the_file_limit_is_refused(tmp_path):
    extractor = _stub(tmp_path, file_limit_bytes=1000)
    with pytest.raises(MediaUnreadable):
        await extractor.extract_windows("oversize", [WindowSpec(0, 0, 10)], str(tmp_path / "out"), wall_s=30, container=WEBM)


async def test_a_child_error_is_an_unreadable_file(tmp_path):
    with pytest.raises(MediaUnreadable):
        await _stub(tmp_path).probe("other")


async def test_a_reported_window_outside_the_request_is_refused(tmp_path):
    with pytest.raises(MediaUnreadable):
        await _stub(tmp_path).extract_windows("ok", [WindowSpec(5, 0, 10)], str(tmp_path / "out"), wall_s=30, container=WEBM)


# =============================================================================
# Real ffprobe / ffmpeg
# =============================================================================

@pytest.mark.slow
@needs_ffmpeg
async def test_probe_and_windows_over_https(tmp_path):
    mp4 = make_audio_mp4(tmp_path / "a.mp4", 100)
    with MediaServer(tmp_path, {"/a.mp4": mp4}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        outcome = await extractor.probe(server.url("/a.mp4"))
        probe = outcome.probe
        assert outcome.status is MediaStatus.OK and probe.has_audio and probe.duration_known
        assert probe.duration_s == pytest.approx(100.0, abs=1.0) and "mp4" in probe.container
        # Plan-shaped: the request's last window is the file's last (M2: it is read at a full window's length, and a
        # header that is right ends it where the plan does, so no eof is reported).
        windows = [WindowSpec(0, 0.0, 40.0), WindowSpec(1, 30.0, 70.0)]
        result = await extractor.extract_windows(server.url("/a.mp4"), windows, str(tmp_path / "o"), wall_s=60,
                                                 container=probe.container)
        assert result.status is MediaStatus.OK and not result.eof
        assert [(w.index, w.start_s) for w, _ in result.windows] == [(0, 0.0), (1, 30.0)]
        assert [w.duration_s for w, _ in result.windows] == [pytest.approx(40.0, abs=0.5),
                                                             pytest.approx(70.0, abs=0.5)]   # measured, not planned
        for _, path in result.windows:
            assert Path(path).read_bytes()[:4] == b"OggS"
        assert any(r[2] for r in server.requests)                # input-side -ss read through Range requests


@pytest.mark.slow
@needs_ffmpeg
async def test_audio_less_file_probes_without_audio(tmp_path):
    import subprocess
    path = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=16x16:r=1",
                    "-t", "3", "-c:v", "libx264", str(path)], check=True)
    with MediaServer(tmp_path, {"/v.mp4": path.read_bytes()}) as server:
        outcome = await SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path)).probe(
            server.url("/v.mp4"))
    assert outcome.probe.has_audio is False


@pytest.mark.slow
@needs_ffmpeg
async def test_probe_without_duration_takes_one_pass_segment(tmp_path):
    """A browser-recorded webm (written to a pipe) has no Duration and no Cues: not an error (G35). The design is the
    local-intermediate pass of T-R15 (not a segment muxer): the first pass of the standard plan reads the file once,
    hits its end, and reports the duration."""
    webm = make_cueless_webm(100)
    with MediaServer(tmp_path, {"/rec.webm": webm}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        outcome = await extractor.probe(server.url("/rec.webm"))
        probe = outcome.probe
        assert outcome.status is MediaStatus.OK and probe.has_audio and not probe.duration_known
        assert probe.duration_s == 0.0 and "webm" in probe.container
        plan = plan_windows(14_400)[:3]
        result = await extractor.extract_windows(server.url("/rec.webm"), plan, str(tmp_path / "o"), wall_s=60,
                                                 container=probe.container, duration_known=False)
    assert result.status is MediaStatus.OK and result.eof and not result.reached_bound
    assert result.duration_s == pytest.approx(100.0, abs=1.0)
    ((spec, path),) = result.windows                              # the others begin past the end of the file
    assert spec.index == 0 and spec.start_s == 0.0 and spec.duration_s == pytest.approx(100.0, abs=1.0)
    assert Path(path).read_bytes()[:4] == b"OggS" and sorted(p.name for p in (tmp_path / "o").iterdir()) == [
        "window_0.opus"]                                          # pass.opus is gone


@pytest.mark.slow
@needs_ffmpeg
async def test_a_pass_cuts_overlapping_windows_whose_content_matches_the_clock(tmp_path):
    """Windows cut from one pass keep the plan's overlap and each holds the media time it claims (pitch = f(time))."""
    webm = make_stepped_tone_webm(100)
    plan = plan_windows(100.0, window_s=40, overlap_s=10)
    assert [(w.index, w.start_s) for w in plan] == [(0, 0.0), (1, 30.0), (2, 60.0)]
    with MediaServer(tmp_path, {"/rec.webm": webm}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        result = await extractor.extract_windows(server.url("/rec.webm"), plan, str(tmp_path / "o"), wall_s=60,
                                                 container=WEBM, duration_known=False)
    assert result.status is MediaStatus.OK and not result.eof and [w.index for w, _ in result.windows] == [0, 1, 2]
    for spec, path in result.windows:
        for abs_t in (5.0 + 10 * k for k in range(10)):           # the middle of every 10 s step
            rel = abs_t - spec.start_s
            if 0 <= rel <= spec.duration_s - 0.6:
                expected = 300 + 100 * int(abs_t // 10)
                assert tone_hz_at(path, rel) == pytest.approx(expected, abs=25), (spec, abs_t)


@pytest.mark.slow
@needs_ffmpeg
async def test_a_resumed_pass_starts_at_the_slice_and_keeps_the_clock(tmp_path):
    webm = make_stepped_tone_webm(100)
    plan = plan_windows(100.0, window_s=40, overlap_s=10)[1:]     # indexes 1 and 2: the first window is done
    with MediaServer(tmp_path, {"/rec.webm": webm}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        result = await extractor.extract_windows(server.url("/rec.webm"), plan, str(tmp_path / "o"), wall_s=60,
                                                 container=WEBM, duration_known=False)
    assert [(w.index, w.start_s) for w, _ in result.windows] == [(1, 30.0), (2, 60.0)]
    (spec1, path1), (spec2, path2) = result.windows
    assert tone_hz_at(path1, 5.0) == pytest.approx(300 + 100 * 3, abs=25)     # abs 35 s
    assert tone_hz_at(path2, 5.0) == pytest.approx(300 + 100 * 6, abs=25)     # abs 65 s
    assert not result.eof and spec2.duration_s == pytest.approx(40.0, abs=0.5)


@pytest.mark.slow
@needs_ffmpeg
def test_a_pass_that_reaches_the_bound_says_so(tmp_path, monkeypatch):
    """The file goes on past the last planned window and that window ends at TRANSCRIBE_MAX_SECONDS: the `too_long`
    stub (child-side runner, called in process with the bound shrunk)."""
    webm = make_cueless_webm(150)
    monkeypatch.setattr(media, "TRANSCRIBE_MAX_SECONDS", 100)
    plan = plan_windows(100.0, window_s=40, overlap_s=10)
    out = tmp_path / "o"
    out.mkdir()
    with MediaServer(tmp_path, {"/rec.webm": webm}) as server:
        result = media.run_windows({"url": server.url("/rec.webm"), "ca_file": server.ca_pem, "container": WEBM,
                                    "duration_known": False, "out_dir": str(out), "wall_s": 60,
                                    "windows": [{"index": w.index, "start_s": w.start_s, "duration_s": w.duration_s}
                                                for w in plan]})
    assert result["status"] == "ok" and result["reached_bound"] is True and result["eof"] is False
    assert [w["index"] for w in result["windows"]] == [0, 1, 2]


@pytest.mark.timing   # the outcome is set by a 3 CPU-s RLIMIT_CPU against decode speed (owner rule 2026-10-20, T-R33)
@needs_ffmpeg
async def test_a_pass_cut_by_the_cpu_limit_returns_the_windows_inside_the_prefix(tmp_path):
    """SIGXCPU ends the pass-1 ffmpeg; the killed Ogg prefix is valid, so the windows fully inside it come back as a
    TIMEOUT prefix and the caller resumes at the first missing one."""
    webm = make_cueless_webm(1_500)
    plan = [WindowSpec(0, 0.0, 60.0), WindowSpec(1, 50.0, 700.0), WindowSpec(2, 740.0, 760.0)]
    with MediaServer(tmp_path, {"/rec.webm": webm}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path), cpu_limit_s=3)
        result = await extractor.extract_windows(server.url("/rec.webm"), plan, str(tmp_path / "o"), wall_s=120,
                                                 container=WEBM, duration_known=False)
    assert result.status is MediaStatus.TIMEOUT and not result.eof
    assert 0 < len(result.windows) < 3 and result.windows[0][0].index == 0
    assert all(Path(p).read_bytes()[:4] == b"OggS" for _, p in result.windows)


@pytest.mark.slow
@needs_ffmpeg
async def test_a_pass_cut_by_the_file_size_limit_returns_the_windows_inside_the_prefix(tmp_path):
    """RLIMIT_FSIZE ends pass 1 the same way (the write fails): the prefix is cut, not discarded."""
    webm = make_cueless_webm(1_500)
    plan = [WindowSpec(0, 0.0, 30.0), WindowSpec(1, 20.0, 700.0), WindowSpec(2, 710.0, 790.0)]
    with MediaServer(tmp_path, {"/rec.webm": webm}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path),
                                             file_limit_bytes=200_000)
        result = await extractor.extract_windows(server.url("/rec.webm"), plan, str(tmp_path / "o"), wall_s=120,
                                                 container=WEBM, duration_known=False)
    assert result.status is MediaStatus.TIMEOUT and not result.eof
    assert 0 < len(result.windows) < 3 and result.windows[0][0].index == 0


@pytest.mark.slow
@needs_ffmpeg
async def test_a_dropped_connection_is_retried_and_the_duration_is_the_whole_files(tmp_path):
    """The first connection dies at 30 %: ffmpeg reconnects with a Range read, so the pass sees the real end (N1)."""
    webm = make_cueless_webm(300)
    plan = plan_windows(14_400)[:3]
    with MediaServer(tmp_path, {"/rec.webm": webm}) as server:
        server.drop_at, server.drop_first_only = int(len(webm) * 0.3), True
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        result = await extractor.extract_windows(server.url("/rec.webm"), plan, str(tmp_path / "o"), wall_s=120,
                                                 container=WEBM, duration_known=False)
    assert result.status is MediaStatus.OK and result.eof
    assert result.duration_s == pytest.approx(300.0, abs=2.0)


@pytest.mark.slow
@needs_ffmpeg
async def test_a_connection_that_keeps_dropping_is_an_error_never_an_eof(tmp_path):
    webm = make_cueless_webm(300)
    plan = plan_windows(14_400)[:3]
    with MediaServer(tmp_path, {"/rec.webm": webm}) as server:
        server.drop_at = int(len(webm) * 0.3)
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        with pytest.raises(MediaUnreadable):
            await extractor.extract_windows(server.url("/rec.webm"), plan, str(tmp_path / "o"), wall_s=120,
                                            container=WEBM, duration_known=False)
    assert not list((tmp_path / "o").glob("window_*"))


@pytest.mark.slow
@needs_ffmpeg
async def test_a_known_duration_window_cut_short_by_a_drop_is_not_reported_at_its_planned_length(tmp_path):
    """Per-window runs rely on `-xerror` and also measure the produced window against the plan (N1)."""
    webm = make_seekable_webm(tmp_path / "a.webm", 100)
    with MediaServer(tmp_path, {"/a.webm": webm}) as server:
        server.drop_at = int(len(webm) * 0.5)
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        with pytest.raises(MediaUnreadable):
            await extractor.extract_windows(server.url("/a.webm"), [WindowSpec(0, 0.0, 90.0)], str(tmp_path / "o"),
                                            wall_s=60, container=WEBM)
    assert not list((tmp_path / "o").glob("window_*"))


@pytest.mark.timing   # the outcome is set by a 3 CPU-s RLIMIT_CPU against decode speed (owner rule 2026-10-20, T-R33)
@needs_ffmpeg
async def test_a_limit_before_the_first_window_is_an_error_not_a_loop(tmp_path):
    """N2: a CPU cut that finishes no window would repeat forever as a TIMEOUT; it is the file's error."""
    webm = make_cueless_webm(3_600)
    with MediaServer(tmp_path, {"/rec.webm": webm}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path), cpu_limit_s=3)
        with pytest.raises(MediaUnreadable):
            await extractor.extract_windows(server.url("/rec.webm"), plan_windows(14_400)[:3], str(tmp_path / "o"),
                                            wall_s=170, container=WEBM, duration_known=False)


@pytest.mark.slow
@needs_ffmpeg
async def test_video_longer_than_the_audio_ends_at_the_audio(tmp_path):
    """The header says 100 s, the audio is 60 s (T-R16): the window keeps its measured length and the file's end is
    the audio's end, not an error."""
    mp4 = make_overstated_mp4(tmp_path / "v.mp4", video_s=100, audio_s=60)
    with MediaServer(tmp_path, {"/v.mp4": mp4}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        probe = (await extractor.probe(server.url("/v.mp4"))).probe
        assert probe.duration_s == pytest.approx(100.0, abs=1.0)
        plan = [WindowSpec(0, 0.0, 40.0), WindowSpec(1, 30.0, 40.0), WindowSpec(2, 60.0, 40.0)]
        result = await extractor.extract_windows(server.url("/v.mp4"), plan, str(tmp_path / "o"), wall_s=60,
                                                 container=probe.container)
    assert result.status is MediaStatus.OK and result.eof and result.duration_s == pytest.approx(60.0, abs=1.0)
    assert [w.index for w, _ in result.windows] == [0, 1]                 # window 2 was never run
    assert result.windows[1][0].duration_s == pytest.approx(30.0, abs=1.0)


@pytest.mark.slow
@needs_ffmpeg
async def test_an_mp3_whose_header_overstates_the_duration_ends_at_the_audio(tmp_path):
    mp3 = make_overstated_mp3(tmp_path / "a.mp3", 120)
    with MediaServer(tmp_path, {"/a.mp3": mp3}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        probe = (await extractor.probe(server.url("/a.mp3"))).probe
        if probe.duration_s < 122:
            pytest.skip("this ffmpeg estimates the duration exactly")
        result = await extractor.extract_windows(server.url("/a.mp3"), [WindowSpec(0, 0.0, probe.duration_s)],
                                                 str(tmp_path / "o"), wall_s=60, container=probe.container)
    assert result.status is MediaStatus.OK and result.eof and result.duration_s == pytest.approx(120.0, abs=2.0)
    assert len(result.windows) == 1


@pytest.mark.slow
@needs_ffmpeg
async def test_one_corrupt_packet_does_not_fail_the_file(tmp_path):
    """`-xerror` makes the glitch fatal (rc 183 on ffmpeg 7.1 / 8.0; exit 0 with a short output on 6.1); the re-run
    without it conceals it (T-R16)."""
    mp4 = make_corrupt_mp4(tmp_path / "c.mp4", 120)
    with MediaServer(tmp_path, {"/c.mp4": mp4}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        probe = (await extractor.probe(server.url("/c.mp4"))).probe
        result = await extractor.extract_windows(server.url("/c.mp4"), [WindowSpec(0, 0.0, 120.0)],
                                                 str(tmp_path / "o"), wall_s=60, container=probe.container)
    assert result.status is MediaStatus.OK and len(result.windows) == 1 and not result.eof


@pytest.mark.timing   # each run must stay under a 3 CPU-s RLIMIT_CPU (owner rule 2026-10-20, T-R33)
@needs_ffmpeg
async def test_a_corrupt_packet_late_in_a_long_file_is_re_run_after_earlier_windows_spent_cpu(tmp_path):
    """N7: the re-run decision judges the failed run's own CPU, not the CPU every earlier window of the step spent.
    15 min of AAC, 2 KB overwritten at 90 %, 60 s windows, a 3 s CPU limit the earlier windows exceed together."""
    mp4 = make_corrupt_mp4(tmp_path / "c.mp4", 900, at=0.9)
    plan = plan_windows(900.0, window_s=60, overlap_s=5)
    with MediaServer(tmp_path, {"/c.mp4": mp4}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path), cpu_limit_s=3)
        probe = (await extractor.probe(server.url("/c.mp4"))).probe
        result = await extractor.extract_windows(server.url("/c.mp4"), plan, str(tmp_path / "o"), wall_s=170,
                                                 container=probe.container)
    assert result.status is MediaStatus.OK and not result.eof
    assert [w.index for w, _ in result.windows] == [w.index for w in plan]


@pytest.mark.slow
@needs_ffmpeg
async def test_a_corrupt_cluster_in_a_pass_is_re_run_and_yields_its_windows(tmp_path, monkeypatch):
    """The pass path: `-xerror` makes the corrupt cluster fatal; the re-run without it conceals it and the pass still
    cuts every planned window (T-R16, N7). Through the extractor, then child-side in process to see the two runs."""
    mkv = make_corrupt_cueless_mkv(150, at=0.5)
    plan = plan_windows(150.0, window_s=60, overlap_s=10)
    with MediaServer(tmp_path, {"/rec.mkv": mkv}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        probe = (await extractor.probe(server.url("/rec.mkv"))).probe
        assert not probe.duration_known and probe.container == WEBM
        result = await extractor.extract_windows(server.url("/rec.mkv"), plan, str(tmp_path / "o"), wall_s=120,
                                                 container=probe.container, duration_known=False)
        assert result.status is MediaStatus.OK
        assert [w.index for w, _ in result.windows] == [w.index for w in plan]

        remote_runs = []
        produced_s = []       # seconds of audio each remote run left in its output
        real = media._run_ffmpeg

        def spy(argv, deadline):
            outcome = real(argv, deadline)
            if "-xerror" in argv or "https" in argv[argv.index("-protocol_whitelist") + 1]:
                remote_runs.append(("-xerror" in argv, outcome[0]))
                out_path = argv[-1]
                produced_s.append(media._file_duration(out_path)
                                  if os.path.isfile(out_path) and os.path.getsize(out_path) else 0.0)
            return outcome

        monkeypatch.setattr(media, "_run_ffmpeg", spy)
        out = tmp_path / "o2"
        out.mkdir()
        child = media.run_windows({"url": server.url("/rec.mkv"), "ca_file": server.ca_pem, "container": WEBM,
                                   "duration_known": False, "out_dir": str(out), "wall_s": 120,
                                   "windows": [{"index": w.index, "start_s": w.start_s, "duration_s": w.duration_s}
                                               for w in plan]})
    # The corrupt cluster under -xerror usually ends the first run: exit 183 on ffmpeg 7.1 / 8.0, exit 0 with a short
    # output and an error logged on 6.1. Either way it is re-run once (T-R16), and the re-run conceals it. The noise is
    # unseeded, so on some draws the garbage lands where ffmpeg conceals it even under -xerror (2 of 60 on 8.0.1: exit 0,
    # the full 150 s): that run stands and is not re-run. The windows must be complete either way.
    span = plan[-1].start_s + plan[-1].duration_s - plan[0].start_s
    assert remote_runs[0][0] is True and remote_runs[0][1] is not None
    first_failed_or_short = remote_runs[0][1] != 0 or produced_s[0] < span - 1.0
    assert remote_runs[1:] == ([(False, 0)] if first_failed_or_short else []), (remote_runs, produced_s)
    assert child["status"] == "ok" and [w["index"] for w in child["windows"]] == [w.index for w in plan]


# -- An -xerror run that only looks clean (ffmpeg 6.1) ---------------------------------------------------------------
# ffmpeg 6.1 ends an `-xerror` run at a corrupt packet with exit 0 and the output so far; 7.1 and 8.0 exit 183. The
# fake ffmpeg below plays each behaviour so the rule is pinned whatever ffmpeg the machine has (measured outputs:
# ffmpeg-versions-report.md).

@pytest.fixture
def stderr_file(tmp_path):
    """fd 2 is a regular file, as in the child (the parent's stderr file)."""
    saved = os.dup(2)
    with open(tmp_path / "stderr.txt", "w+b") as fh:
        os.dup2(fh.fileno(), 2)
        try:
            yield
        finally:
            os.dup2(saved, 2)
            os.close(saved)


def _fake_runs(monkeypatch, behaviour):
    """`behaviour(xerror, argv) -> (code, produced_s, logged)`; records (xerror, code) per remote run (a pass's local
    cuts are not recorded)."""
    runs = []

    def run(argv, deadline):
        xerror = "-xerror" in argv
        code, produced, logged = behaviour(xerror, argv)
        if logged:
            os.write(2, b"[aac @ 0x1] Error submitting packet to decoder: Invalid data found when processing input\n")
        with open(argv[-1], "wb") as fh:
            fh.write(b"OggS" + str(produced).encode())
        if "https" in argv[argv.index("-protocol_whitelist") + 1]:
            runs.append((xerror, code))
        return code, False, 0.1

    def duration(path, deadline=None):
        with open(path, "rb") as fh:
            return float(fh.read()[4:])

    monkeypatch.setattr(media, "_run_ffmpeg", run)
    monkeypatch.setattr(media, "_file_duration", duration)
    return runs


def _windows_request(tmp_path, windows, duration_known=True):
    return {"url": "https://h/f", "ca_file": "/x/ca.pem", "container": "mp3", "out_dir": str(tmp_path),
            "wall_s": 60.0, "duration_known": duration_known,
            "windows": [{"index": i, "start_s": st, "duration_s": d} for i, st, d in windows]}


@pytest.mark.parametrize("first_code", [0, 183], ids=["ffmpeg-6.1", "ffmpeg-7.1-8.0"])
def test_an_xerror_run_cut_at_a_corrupt_packet_is_re_run_whatever_its_exit(tmp_path, monkeypatch, stderr_file,
                                                                             first_code):
    """120 s file, one window: the -xerror run stops at 58 s with an error logged; never an eof at 58 s."""
    runs = _fake_runs(monkeypatch, lambda xerror, argv: (first_code, 58.0, True) if xerror else (0, 120.0, True))
    result = media.run_windows(_windows_request(tmp_path, [(0, 0.0, 120.0)]))
    assert runs == [(True, first_code), (False, 0)]
    assert result["status"] == "ok" and result["eof"] is False
    assert [(w["index"], w["duration_s"]) for w in result["windows"]] == [(0, 120.0)]


@pytest.mark.parametrize("first_code", [0, 183], ids=["ffmpeg-6.1", "ffmpeg-7.1-8.0"])
def test_a_pass_cut_at_a_corrupt_cluster_is_re_run_and_keeps_its_later_windows(tmp_path, monkeypatch, stderr_file,
                                                                                first_code):
    """A 150 s file without a duration, one pass of three windows: the -xerror pass stops at 75 s with an error
    logged; the windows after it are not dropped and no eof is reported at 75 s."""
    def behaviour(xerror, argv):
        if "https" not in argv[argv.index("-protocol_whitelist") + 1]:       # a local cut of the pass file
            return 0, float(argv[argv.index("-t") + 1]), False
        return (first_code, 75.0, True) if xerror else (0, 150.0, True)
    runs = _fake_runs(monkeypatch, behaviour)
    windows = [(0, 0.0, 60.0), (1, 50.0, 60.0), (2, 100.0, 50.0)]
    result = media.run_windows(_windows_request(tmp_path, windows, duration_known=False))
    assert runs == [(True, first_code), (False, 0)]
    assert result["status"] == "ok" and result["eof"] is False
    assert [w["index"] for w in result["windows"]] == [0, 1, 2]


def test_a_clean_short_run_with_nothing_logged_is_the_real_end(tmp_path, monkeypatch, stderr_file):
    """The audio ends at 58 s (header overstated it): exit 0, nothing logged, no re-run, eof at the measured end."""
    runs = _fake_runs(monkeypatch, lambda xerror, argv: (0, 58.0, False))
    result = media.run_windows(_windows_request(tmp_path, [(0, 0.0, 120.0)]))
    assert runs == [(True, 0)]
    assert result["eof"] is True and result["duration_s"] == pytest.approx(58.0)


def test_an_error_logged_by_a_run_that_reaches_its_bound_is_no_reason_to_re_run(tmp_path, monkeypatch, stderr_file):
    """ffmpeg 8.0 logs an Opus packet-header error on a sound stream: a full-length window stands as it is."""
    runs = _fake_runs(monkeypatch, lambda xerror, argv: (0, float(argv[argv.index("-t") + 1]), True))
    result = media.run_windows(_windows_request(tmp_path, [(0, 0.0, 60.0), (1, 50.0, 60.0)]))
    assert runs == [(True, 0), (True, 0)]
    assert [w["index"] for w in result["windows"]] == [0, 1]


def test_unknown_stderr_counts_as_logged():
    """stderr that is not a regular file cannot be measured: a short exit-0 run is then re-run, never trusted."""
    assert media._logged_since(None) is True
    read_end, write_end = os.pipe()
    saved = os.dup(2)
    try:
        os.dup2(write_end, 2)
        assert media._stderr_size() is None and media._logged_since(0) is True
    finally:
        os.dup2(saved, 2)
        for fd in (saved, read_end, write_end):
            os.close(fd)


def test_a_limit_is_judged_on_the_runs_own_cpu_not_the_childs_total(tmp_path, monkeypatch):
    """Two sequential runs of 0.6 x cpu_s each: together over 0.9 x cpu_s, neither alone. RLIMIT_CPU is per process,
    so neither run was cut by it; the other run's 0.95 x cpu_s was."""
    clock = {"cpu": 0.0}
    monkeypatch.setattr(media.resource, "getrusage",
                        lambda who: SimpleNamespace(ru_utime=clock["cpu"], ru_stime=0.0))

    def fake_run(argv, **kwargs):
        clock["cpu"] += float(argv[0])
        return SimpleNamespace(returncode=183)

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    request = {"cpu_s": 10, "file_bytes": 0}
    out = str(tmp_path / "missing.opus")
    deadline = time.monotonic() + 60
    for _ in range(2):
        code, timed_out, cpu = media._run_ffmpeg(["6.0"], deadline)
        assert (code, timed_out, cpu) == (183, False, pytest.approx(6.0))
        assert not media._hit_a_limit(request, out, cpu)
    assert clock["cpu"] >= 0.9 * request["cpu_s"]               # the summed total would have read as a limit
    code, _, cpu = media._run_ffmpeg(["9.5"], deadline)
    assert media._hit_a_limit(request, out, cpu)


def test_network_exit_codes_are_computed_for_this_platform():
    import errno
    codes = media.network_exit_codes()
    assert (-errno.EIO) & 0xFF in codes and (-errno.ECONNRESET) & 0xFF in codes
    assert 0 not in codes and all(0 < c < 256 for c in codes)


async def test_an_empty_container_is_refused_before_any_child_runs(tmp_path):
    for bad in ("", "-i x"):
        with pytest.raises(MediaUnreadable):
            await _stub(tmp_path).extract_windows("ok", [WindowSpec(0, 0, 10)], str(tmp_path / "o"), wall_s=30,
                                                  container=bad)


async def test_a_pass_takes_a_short_contiguous_slice(tmp_path):
    with pytest.raises(ValueError):
        await _stub(tmp_path).extract_windows("ok", [WindowSpec(i, 0, 10) for i in range(4)], str(tmp_path / "o"),
                                              wall_s=30, container=WEBM, duration_known=False)
    with pytest.raises(ValueError):
        await _stub(tmp_path).extract_windows("ok", [WindowSpec(0, 0, 10), WindowSpec(2, 0, 10)],
                                              str(tmp_path / "o"), wall_s=30, container=WEBM, duration_known=False)


@pytest.mark.slow
@needs_ffmpeg
async def test_tls_verify_refuses_an_untrusted_server(tmp_path):
    """ffmpeg verifies nothing by default; the argv makes it. A server whose CA is not the one given is refused."""
    mp4 = make_audio_mp4(tmp_path / "a.mp4", 10)
    other = tmp_path / "other"
    other.mkdir()
    with MediaServer(tmp_path, {"/a.mp4": mp4}) as server, MediaServer(other) as stranger:
        extractor = SubprocessMediaExtractor(ca_file=stranger.ca_pem, temp_root=str(tmp_path))
        with pytest.raises(MediaUnreadable):
            await extractor.probe(server.url("/a.mp4"))
        trusted = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        assert (await trusted.probe(server.url("/a.mp4"))).probe is not None


@pytest.mark.timing   # the outcome is set by a 1.5 s wall against decode speed (owner rule 2026-10-20, T-R33)
@needs_ffmpeg
def test_a_wall_in_a_window_returns_the_windows_finished_before_it(tmp_path):
    """The child stops its own ffmpeg before the parent's kill: finished windows come back, in order, and a window cut
    by the wall leaves no file. Child-side runner, called in process with a short wall."""
    mp4 = make_audio_mp4(tmp_path / "a.mp4", 30)
    out = tmp_path / "o"
    out.mkdir()
    windows = [{"index": i, "start_s": float(i % 20), "duration_s": 10.0} for i in range(60)]
    with MediaServer(tmp_path, {"/a.mp4": mp4}) as server:
        result = media.run_windows({"url": server.url("/a.mp4"), "ca_file": server.ca_pem, "container": "mov,mp4,m4a,3gp,3g2,mj2",
                                    "windows": windows, "out_dir": str(out), "wall_s": 1.5})
    assert result["status"] == "timeout" and 0 < len(result["windows"]) < 60
    assert [w["index"] for w in result["windows"]] == list(range(len(result["windows"])))
    assert sorted(p.name for p in out.iterdir()) == sorted(f"window_{w['index']}.opus" for w in result["windows"])
