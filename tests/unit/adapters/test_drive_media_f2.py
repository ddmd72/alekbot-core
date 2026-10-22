"""Final-review fixes for the media child (T-R32, M1-M4, M7): more demuxers, a short window that is not the last,
a header that understates the audio, the child's group killed after it exits, the process limit, the bounded probe.
Real ffmpeg where a file can be built; the child-side runner is called in process with its ffmpeg calls faked
where the case (a clean short window mid-file) cannot be built from a real container."""
import asyncio
import os
import resource
import subprocess
import sys
import time

import pytest

import src.adapters.drive_parser.child as child
import src.adapters.drive_parser.media as media
import src.adapters.drive_parser.media_launch as launch
from src.adapters.drive_parser.media_extractor import SubprocessMediaExtractor
from src.domain.drive_index_params import TRANSCRIBE_WINDOW_S
from src.domain.drive_transcript import MediaStatus, MediaUnreadable, WindowSpec
from tests.unit.adapters.drive_media_fixtures import HAVE_FFMPEG, MediaServer, _ffmpeg

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


def _make(tmp_path, demuxer):
    path = tmp_path / f"t.{demuxer}"
    sine = ["-f", "lavfi", "-i", "sine=frequency=440:duration=20"]
    if demuxer == "avi":
        _ffmpeg(*sine, "-c:a", "pcm_s16le", str(path))
    elif demuxer == "asf":
        _ffmpeg(*sine, "-c:a", "wmav2", str(path))
    elif demuxer == "aiff":
        _ffmpeg(*sine, str(path))
    elif demuxer == "flv":
        _ffmpeg(*sine, "-c:a", "mp3", str(path))
    else:                                   # AMR-NB: no encoder here; 1 000 silent 12.2 kbit/s frames = 20 s
        path.write_bytes(b"#!AMR\n" + (bytes([0x3C]) + bytes(31)) * 1000)
    return path.read_bytes()


@pytest.mark.slow
@needs_ffmpeg
@pytest.mark.parametrize("demuxer", ["avi", "asf", "aiff", "amr", "flv"])
async def test_a_file_of_each_added_container_probes_and_yields_one_window(tmp_path, demuxer):
    data = _make(tmp_path, demuxer)
    with MediaServer(tmp_path, {"/f": data, "/list.m3u8": b"#EXTM3U\n#EXTINF:10,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n",
                                "/concat.txt": b"ffconcat version 1.0\nfile 'file:///etc/passwd'\n"}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        probe = (await extractor.probe(server.url("/f"))).probe
        assert probe.has_audio and probe.duration_s == pytest.approx(20.0, abs=1.0)
        assert demuxer in probe.container.split(",")
        result = await extractor.extract_windows(server.url("/f"), [WindowSpec(0, 0.0, probe.duration_s)],
                                                 str(tmp_path / "o"), wall_s=60, container=probe.container)
        assert result.status is MediaStatus.OK and len(result.windows) == 1
        # a nested-resource attempt stays refused even with an admitted demuxer pinned
        for bad in ("/list.m3u8", "/concat.txt"):
            with pytest.raises(MediaUnreadable):
                await extractor.extract_windows(server.url(bad), [WindowSpec(0, 0.0, 10.0)], str(tmp_path / "p"),
                                                wall_s=60, container=demuxer)
            with pytest.raises(MediaUnreadable):
                await extractor.probe(server.url(bad))


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

def _mp3_understating(tmp_path):
    """60 s of noise then 60 s of tone, VBR without a Xing header: the duration is estimated from the first (noisy,
    high-bitrate) frame, so the container reports well under the real 120 s."""
    path = tmp_path / "under.mp3"
    _ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=60:a=0.5", "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
            "-filter_complex", "[0][1]concat=n=2:v=0:a=1", "-c:a", "libmp3lame", "-q:a", "5", "-write_xing", "0",
            str(path))
    return path.read_bytes()


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


@pytest.mark.slow
@needs_ffmpeg
async def test_a_header_that_understates_the_audio_does_not_cut_the_last_window_short(tmp_path):
    data = _mp3_understating(tmp_path)
    with MediaServer(tmp_path, {"/u.mp3": data}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        probe = (await extractor.probe(server.url("/u.mp3"))).probe
        if probe.duration_s > 110:
            pytest.skip("this ffmpeg reads the duration exactly")
        result = await extractor.extract_windows(server.url("/u.mp3"), [WindowSpec(0, 0.0, probe.duration_s)],
                                                 str(tmp_path / "o"), wall_s=60, container=probe.container)
    assert result.status is MediaStatus.OK and result.eof
    assert result.duration_s == pytest.approx(120.0, abs=3.0)
    ((spec, _),) = result.windows
    assert spec.duration_s == pytest.approx(120.0, abs=3.0)


# -- M3: the group is killed after the child exits; the process limit -------------------------------------------

async def test_the_process_group_is_killed_after_a_normal_exit_too(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(launch, "kill_group", lambda pid: killed.append(pid))
    with open(tmp_path / "err", "w+b") as errors:
        code, timed_out = await launch.spawn({"x": 1}, str(tmp_path), errors, python=sys.executable,
                                             module="json.tool", wall_s=30.0, sandbox=None)
    assert code == 0 and not timed_out and len(killed) == 1


def test_the_child_applies_a_process_limit_when_asked(monkeypatch):
    calls = {}
    monkeypatch.setattr(resource, "setrlimit", lambda which, value: calls.__setitem__(which, value))
    child.apply_limits(1 << 30, 100, 1 << 20, nproc=256)
    assert calls[resource.RLIMIT_NPROC] == (256, 256)
    calls.clear()
    child.apply_limits(1 << 30, 100, 1 << 20)
    assert resource.RLIMIT_NPROC not in calls


async def test_the_process_limit_is_requested_only_for_the_unprivileged_user(tmp_path, monkeypatch):
    requests = []

    async def spawn(request, workdir, stderr, **kw):
        requests.append(dict(request))
        return 0, False
    monkeypatch.setattr(launch, "spawn", spawn)
    monkeypatch.setattr(os, "chown", lambda *a: None)
    for sandbox in (None, (1234, 1234)):
        extractor = SubprocessMediaExtractor(ca_file="/ca", temp_root=str(tmp_path))
        extractor._sandbox = sandbox
        with pytest.raises(MediaUnreadable):
            await extractor.probe("https://x.invalid/f")
    assert "nproc" not in requests[0] and requests[1]["nproc"] == media_nproc()


def media_nproc():
    from src.adapters.drive_parser.media_extractor import MEDIA_NPROC_LIMIT
    return MEDIA_NPROC_LIMIT


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
