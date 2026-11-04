"""The media child's runner, `drive_parser/media.py` (USER_DRIVE_INDEXING_RFC §27.4, Δ99, Δ134; plan G14, G15, G35),
called in process: the bound, the re-run rules and the wall, with real ffmpeg against a local HTTPS server with a test
CA (skipped without ffmpeg) or with ffmpeg faked. The in-process extractor and its launcher these tests once shared a
file with are gone (S5 card F): the runner now runs only in the sandbox's media child, end to end in
`tests/integration/sandbox/test_media_tunnel_contract.py`."""
import os
import time
from types import SimpleNamespace

import pytest

from src.adapters.drive_parser import media
from src.domain.drive_transcript_windows import plan_windows
from tests.unit.adapters.drive_media_fixtures import (
    HAVE_FFMPEG, MediaServer, make_audio_mp4, make_cueless_webm,
)

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg and ffprobe")
WEBM = "matroska,webm"


# =============================================================================
# Real ffprobe / ffmpeg
# =============================================================================


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
