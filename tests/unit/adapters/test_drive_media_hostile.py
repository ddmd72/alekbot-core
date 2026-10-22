"""Hostile media under the child's real limits, with real ffmpeg (USER_DRIVE_INDEXING_RFC §27.4, Δ99; plan G14): a local
HTTPS server with a test CA, because a plain http server would be refused by `-protocol_whitelist https,…` for every
file, good and hostile alike. Each file ends as a defined outcome, never a hang."""
import os
import shutil
import subprocess
import sys
import time

import pytest

from src.adapters.drive_parser.media_extractor import SubprocessMediaExtractor
from src.domain.drive_transcript import MediaStatus, MediaUnreadable, WindowSpec
from tests.unit.adapters.drive_media_fixtures import HAVE_FFMPEG, MediaServer, make_audio_mp4, make_cueless_webm, make_silent_video_mp4

pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg and ffprobe")
DEFINED_WITHIN_S = 60
MP4 = "mov,mp4,m4a,3gp,3g2,mj2"


@pytest.fixture
def served(tmp_path):
    good = make_audio_mp4(tmp_path / "good.mp4", 20)
    files = {
        "/good.mp4": good,
        # a playlist pointing at the local filesystem and at another host
        "/list.m3u8": (b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nfile:///etc/passwd\n#EXTINF:10,\n"
                       b"https://other.invalid/x.mp3\n#EXT-X-ENDLIST\n"),
        "/concat.txt": b"ffconcat version 1.0\nfile 'file:///etc/passwd'\nfile 'https://other.invalid/y.mp3'\n",
        "/empty.mp4": b"",
        "/rec.webm": make_cueless_webm(20),
        "/truncated.mp4": good[:3000],                    # the moov atom sits at the end of this file
        "/garbage.mp4": bytes(range(256)) * 40,
    }
    with MediaServer(tmp_path, files) as server:
        yield server


def _extractor(server, tmp_path):
    return SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))


@pytest.mark.timing   # asserts a wall-clock bound (DEFINED_WITHIN_S) (owner rule 2026-10-20, T-R33)
@pytest.mark.parametrize("path", ["/list.m3u8", "/concat.txt", "/empty.mp4", "/truncated.mp4", "/garbage.mp4"])
async def test_hostile_files_end_as_a_defined_outcome(served, tmp_path, path):
    extractor = _extractor(served, tmp_path)
    started = time.monotonic()
    with pytest.raises(MediaUnreadable):
        await extractor.probe(served.url(path))
    with pytest.raises(MediaUnreadable):
        await extractor.extract_windows(served.url(path), [WindowSpec(0, 0.0, 10.0)], str(tmp_path / "o"), wall_s=60, container=MP4)
    assert time.monotonic() - started < DEFINED_WITHIN_S
    assert not list((tmp_path / "o").glob("window_*"))
    assert list(tmp_path.glob("drive-media-*")) == []            # the child's directory is gone


@pytest.mark.datafile
@pytest.mark.slow
async def test_a_playlist_is_refused_and_nothing_it_names_is_requested(served, tmp_path):
    """The format whitelist refuses `hls`, so the playlist is read as no allowed demuxer and the only request is for the
    playlist itself. (HLS also refuses `file:` segments on its own, so this is not a pure whitelist control.)"""
    with pytest.raises(MediaUnreadable):
        await _extractor(served, tmp_path).probe(served.url("/list.m3u8"))
    assert {r[1] for r in served.requests} == {"/list.m3u8"}


@pytest.mark.datafile
@pytest.mark.slow
async def test_a_demuxer_outside_the_allowlist_cannot_be_pinned(served, tmp_path):
    with pytest.raises(MediaUnreadable):
        await _extractor(served, tmp_path).extract_windows(
            served.url("/list.m3u8"), [WindowSpec(0, 0.0, 10.0)], str(tmp_path / "o"), wall_s=60, container="hls")


@pytest.mark.datafile
@pytest.mark.slow
async def test_the_good_file_still_works_beside_them(served, tmp_path):
    extractor = _extractor(served, tmp_path)
    probe = (await extractor.probe(served.url("/good.mp4"))).probe
    result = await extractor.extract_windows(served.url("/good.mp4"), [WindowSpec(0, 0.0, 10.0)],
                                             str(tmp_path / "o"), wall_s=60, container=probe.container)
    assert result.status is MediaStatus.OK and len(result.windows) == 1


@pytest.mark.timing   # asserts a wall-clock bound on a real decode (owner rule 2026-10-20, T-R33)
async def test_four_hours_of_silence_probes_long_and_one_window_stays_under_the_limits(tmp_path):
    """A 1x1-ish video with 4 h of silence is cheap to store and costly to decode: the probe says 4 h, and one window
    comes out as a valid Opus file under the child's CPU / file / address-space limits. The window is plan-shaped: a
    window that is not the file's last is always a full TRANSCRIBE_WINDOW_S (1 800 s), and a request's last window is
    read at that length (M2), so this is the size production decodes."""
    big = make_silent_video_mp4(tmp_path / "big.mp4", 14_400)
    with MediaServer(tmp_path, {"/big.mp4": big}) as server:
        extractor = SubprocessMediaExtractor(ca_file=server.ca_pem, temp_root=str(tmp_path))
        probe = (await extractor.probe(server.url("/big.mp4"))).probe
        assert probe.duration_s == pytest.approx(14_400.0, abs=2.0) and probe.has_audio
        started = time.monotonic()
        result = await extractor.extract_windows(server.url("/big.mp4"), [WindowSpec(7, 7_000.0, 1_800.0)],
                                                 str(tmp_path / "o"), wall_s=120, container=probe.container)
    assert result.status is MediaStatus.OK and time.monotonic() - started < DEFINED_WITHIN_S
    ((spec, path),) = result.windows
    shown = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name:format=duration",
                            "-of", "default=nw=1", path], capture_output=True, text=True, check=True).stdout
    assert "codec_name=opus" in shown and spec.index == 7
    assert float(shown.split("duration=")[1].split()[0]) == pytest.approx(1_800.0, abs=1.0)
    assert spec.duration_s == pytest.approx(1_800.0, abs=1.0) and not result.eof   # audio goes on past it


# =============================================================================
# The built image (docker-gated: set SANDBOX_TEST_IMAGE; run once at acceptance)
# =============================================================================

_IMAGE = os.getenv("SANDBOX_TEST_IMAGE")
_IMAGE_SKIP = pytest.mark.skipif(not _IMAGE or shutil.which("docker") is None,
                                 reason="needs the built indexer image: set SANDBOX_TEST_IMAGE (acceptance only)")
_IMAGE_HOSTILE = """
import asyncio, sys
from src.adapters.drive_parser.media_extractor import SubprocessMediaExtractor
from src.domain.drive_transcript import MediaUnreadable
base, ca = sys.argv[1], sys.argv[2]
async def main():
    ex = SubprocessMediaExtractor(ca_file=ca, sandbox_user=None)
    assert (await ex.probe(base + "/good.mp4")).probe is not None
    for path in ("/list.m3u8", "/concat.txt", "/empty.mp4", "/truncated.mp4"):
        try:
            await ex.probe(base + path)
        except MediaUnreadable:
            continue
        raise SystemExit("hostile file was read: " + path)
asyncio.run(main())
"""


def _docker(*args, user=None, timeout=300):
    cmd = ["docker", "run", "--rm"] + (["--user", user] if user else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@_IMAGE_SKIP
@pytest.mark.docker
def test_image_ffmpeg_supports_flags():
    """The image's own ffmpeg (Debian's, whichever release the base image ships) knows the flags the argv relies on and
    can encode Opus; its release is recorded in the failure message, not assumed."""
    release = _docker("--entrypoint", "cat", _IMAGE, "/etc/os-release").stdout
    full = _docker("--entrypoint", "ffmpeg", _IMAGE, "-hide_banner", "-h", "full").stdout
    for option in ("format_whitelist", "protocol_whitelist", "tls_verify", "ca_file"):
        assert option in full, f"{option} missing from the image's ffmpeg ({release.splitlines()[:2]})"
    encoders = _docker("--entrypoint", "ffmpeg", _IMAGE, "-hide_banner", "-encoders").stdout
    assert "libopus" in encoders


@_IMAGE_SKIP
@pytest.mark.docker
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs --network host (Linux)")
def test_image_honours_tls_verify_and_the_hostile_set_passes_as_the_parser_user(served, tmp_path):
    base = served.url("")
    mount = ["-v", f"{tmp_path}:/ca:ro", "--network", "host"]
    refused = _docker(*mount, "--entrypoint", "ffprobe", _IMAGE, "-v", "error", "-tls_verify", "1", "-ca_file",
                      "/etc/ssl/certs/ca-certificates.crt", "-i", base + "/good.mp4")
    assert refused.returncode != 0                      # a self-signed server without its CA is refused
    accepted = _docker(*mount, "--entrypoint", "ffprobe", _IMAGE, "-v", "error", "-tls_verify", "1", "-ca_file",
                       "/ca/ca.pem", "-i", base + "/good.mp4")
    assert accepted.returncode == 0, accepted.stderr[-200:]
    mismatch = _docker(*mount, "--add-host", "wrong-name.invalid:127.0.0.1", "--entrypoint", "ffprobe", _IMAGE, "-v",
                       "error", "-tls_verify", "1", "-ca_file", "/ca/ca.pem", "-i",
                       base.replace("localhost", "wrong-name.invalid") + "/good.mp4")
    assert mismatch.returncode != 0                     # a trusted CA, the wrong host name: Debian's ffmpeg is GnuTLS
    pipe_webm = _docker(*mount, "--entrypoint", "ffmpeg", _IMAGE, "-nostdin", "-v", "error", "-xerror", "-tls_verify",
                        "1", "-ca_file", "/ca/ca.pem", "-f", "matroska,webm", "-i", base + "/rec.webm", "-f", "null", "-")
    assert pipe_webm.returncode == 0, pipe_webm.stderr[-200:]   # -xerror does not fail a clean cue-less recording
    ran = _docker(*mount, "--entrypoint", "python", _IMAGE, "-c", _IMAGE_HOSTILE, base, "/ca/ca.pem",
                  user="driveparser")
    assert ran.returncode == 0, ran.stderr[-400:]
