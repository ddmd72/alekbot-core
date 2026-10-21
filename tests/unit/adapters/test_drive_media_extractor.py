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
    HAVE_FFMPEG, MediaServer, make_audio_mp4, make_cueless_webm, make_stepped_tone_webm, tone_hz_at,
)

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


async def test_wall_kills_group(tmp_path):
    out = tmp_path / "out"
    extractor = _stub(tmp_path)
    started = time.monotonic()
    outcome = await extractor.extract_windows("sleep_group", [WindowSpec(0, 0, 10)], str(out), wall_s=3, container=WEBM)
    assert outcome.status is MediaStatus.TIMEOUT and outcome.windows == ()
    assert time.monotonic() - started < 15


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

@needs_ffmpeg
async def test_probe_and_windows_over_https(tmp_path):
    mp4 = make_audio_mp4(tmp_path / "a.mp4", 100)
    with MediaServer(tmp_path, {"/a.mp4": mp4}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        outcome = await extractor.probe(server.url("/a.mp4"))
        probe = outcome.probe
        assert outcome.status is MediaStatus.OK and probe.has_audio and probe.duration_known
        assert probe.duration_s == pytest.approx(100.0, abs=1.0) and "mp4" in probe.container
        windows = [WindowSpec(0, 0.0, 40.0), WindowSpec(1, 30.0, 40.0)]
        result = await extractor.extract_windows(server.url("/a.mp4"), windows, str(tmp_path / "o"), wall_s=60,
                                                 container=probe.container)
        assert result.status is MediaStatus.OK and [w[0] for w in result.windows] == windows
        for _, path in result.windows:
            assert Path(path).read_bytes()[:4] == b"OggS"
        assert any(r[2] for r in server.requests)                # input-side -ss read through Range requests


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


@needs_ffmpeg
async def test_probe_without_duration_takes_one_pass_segment(tmp_path):
    """A browser-recorded webm (written to a pipe) has no Duration and no Cues: not an error (G35); the first pass of
    the standard plan reads the file once, hits its end, and reports the duration (T-R15)."""
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
