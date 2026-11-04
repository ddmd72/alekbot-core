"""Final-review fixes for the media child (T-R32, M1-M4, M7): more demuxers, a short window that is not the last,
a header that understates the audio, the process limit, the bounded probe. The child-side runner is called in process
with its ffmpeg calls faked where the case (a clean short window mid-file) cannot be built from a real container; the
real-ffmpeg cases (each added demuxer, the understating header) run end to end through the sandbox in
`tests/integration/sandbox/test_media_tunnel_contract.py` since the in-process extractor is gone (S5 card F)."""
import os
import resource
import subprocess
import time

import pytest

import src.adapters.drive_parser.child as child
import src.adapters.drive_parser.media as media
from src.domain.drive_index_params import TRANSCRIBE_WINDOW_S
from tests.unit.adapters.drive_media_fixtures import HAVE_FFMPEG

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg and ffprobe")
URL, CA = "https://x.invalid/f", "/ca.pem"


def _value(argv, flag):
    return argv[argv.index(flag) + 1]


# -- T-R32 / M4: allowlists ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("demuxer", ["avi", "asf", "aiff", "amr", "flv"])
def test_the_self_contained_demuxers_are_on_the_format_allowlist(demuxer):
    names = _value(media.build_probe_argv(URL, CA), "-format_whitelist").split(",")
    assert demuxer in names
    window = media.build_window_argv(URL, CA, demuxer, 0.0, 10.0, "/w/window_0.opus.part")
    assert demuxer in _value(window, "-format_whitelist").split(",") and _value(window, "-f") == demuxer


def test_no_nested_resource_demuxer_is_allowlisted():
    names = set(media.FORMAT_WHITELIST.split(","))
    assert not names & {"hls", "concat", "dash", "applehttp", "ffconcat", "image2", "sdp", "rtsp", "mpegts"}


def test_the_protocol_allowlist_has_no_crypto():
    for argv in (media.build_probe_argv(URL, CA), media.build_window_argv(URL, CA, "avi", 0, 10, "/o"),
                 media.build_pass_argv(URL, CA, "avi", 0, 10, "/o")):
        assert _value(argv, "-protocol_whitelist").split(",") == ["https", "tls", "tcp"]


# -- M1: a short window that is not the last ----------------------------------------------------------------------

def _fake_ffmpeg(monkeypatch, lengths):
    """`lengths`: window index -> produced length (0 = header-only). Each fake run writes a non-empty file."""
    def run_remote(build, request, deadline, out_path):
        argv = build(True)
        index = int(os.path.basename(out_path).split("_")[1].split(".")[0])
        with open(out_path, "wb") as fh:
            fh.write(b"OggS" + str(index).encode())
        return 0, False, 0.0
    monkeypatch.setattr(media, "_run_remote", run_remote)
    monkeypatch.setattr(media, "_file_duration", lambda path, deadline=None: lengths[
        int(os.path.basename(path).split("_")[1].split(".")[0])])


def _request(tmp_path, windows):
    return {"url": URL, "ca_file": CA, "container": "mp3", "out_dir": str(tmp_path), "wall_s": 60.0,
            "windows": [{"index": i, "start_s": s, "duration_s": d} for i, s, d in windows]}


def test_a_clean_short_window_in_the_middle_is_not_the_end_of_the_file(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, {0: 60.0, 1: 20.0, 2: 60.0, 3: 30.0})
    result = media.run_windows(_request(tmp_path, [(0, 0.0, 60.0), (1, 50.0, 60.0), (2, 100.0, 60.0), (3, 150.0, 30.0)]))
    assert result["status"] == "ok" and [w["index"] for w in result["windows"]] == [0, 1, 2, 3]
    assert result["windows"][1]["duration_s"] == 20.0 and result["eof"] is False


def test_only_a_header_only_window_after_a_short_one_confirms_the_end(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, {0: 60.0, 1: 20.0, 2: 0.0, 3: 0.0})
    result = media.run_windows(_request(tmp_path, [(0, 0.0, 60.0), (1, 50.0, 60.0), (2, 100.0, 60.0), (3, 150.0, 30.0)]))
    assert result["status"] == "ok" and result["eof"] is True
    assert [w["index"] for w in result["windows"]] == [0, 1]
    assert result["duration_s"] == pytest.approx(70.0)          # the last audio actually produced: 50 + 20
    assert not (tmp_path / "window_2.opus").exists()


def test_a_short_last_window_is_still_the_end(tmp_path, monkeypatch):
    _fake_ffmpeg(monkeypatch, {0: 60.0, 1: 25.0})
    result = media.run_windows(_request(tmp_path, [(0, 0.0, 60.0), (1, 50.0, 60.0)]))
    assert result["eof"] is True and result["duration_s"] == pytest.approx(75.0)


# -- M2: the last window is requested at the full window length ---------------------------------------------------


def test_the_last_window_is_asked_for_a_full_window_length(tmp_path, monkeypatch):
    seen = []

    def run_remote(build, request, deadline, out_path):
        seen.append(float(_value(build(True), "-t")))
        with open(out_path, "wb") as fh:
            fh.write(b"OggS")
        return 0, False, 0.0
    monkeypatch.setattr(media, "_run_remote", run_remote)
    monkeypatch.setattr(media, "_file_duration", lambda path, deadline=None: 50.0)
    media.run_windows(_request(tmp_path, [(0, 0.0, float(TRANSCRIBE_WINDOW_S)), (1, 1740.0, 50.0)]))
    assert seen == [float(TRANSCRIBE_WINDOW_S), float(TRANSCRIBE_WINDOW_S)]


# -- M3: the process limit ---------------------------------------------------------------------------------------


def test_the_child_applies_a_process_limit_when_asked(monkeypatch):
    calls = {}
    monkeypatch.setattr(resource, "setrlimit", lambda which, value: calls.__setitem__(which, value))
    child.apply_limits(1 << 30, 100, 1 << 20, nproc=256)
    assert calls[resource.RLIMIT_NPROC] == (256, 256)
    calls.clear()
    child.apply_limits(1 << 30, 100, 1 << 20)
    assert resource.RLIMIT_NPROC not in calls


def test_the_module_doc_states_who_can_read_the_url():
    doc = media.__doc__
    assert "world-readable" in doc and "driveparser" in doc


# -- M7: the duration probe is bounded by the step's deadline ---------------------------------------------------

def test_the_file_duration_probe_is_bounded_by_the_remaining_deadline(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen["timeout"] = kw["timeout"]
        return subprocess.CompletedProcess(argv, 0, stdout=b"12.5\n")
    monkeypatch.setattr(subprocess, "run", run)
    assert media._file_duration("/x.opus", time.monotonic() + 4.0) == 12.5
    assert 0 < seen["timeout"] <= 4.0
    media._file_duration("/x.opus", time.monotonic() - 5.0)
    assert 0 < seen["timeout"] <= 1.0                     # never zero, never the old fixed 30 s
    media._file_duration("/x.opus")
    assert seen["timeout"] == 30
