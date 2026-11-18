"""S5 card D, the end-to-end media contract (UNTRUSTED_CONTENT_SANDBOX_RFC §7 S5; S5 design §2.1-§2.6): the indexer's
media port (`SandboxMediaAdapter`) -> the raw-h2 tunnel client -> the light service as `sandbox_main.build_app` assembles
it (the real caller gate, the tunnel route in front of Quart, the media engines) served by Hypercorn on loopback (h2c)
-> the real media child -> real ffprobe / ffmpeg, on tiny recordings ffmpeg makes at test time. The recordings sit on a
local fake drive (HTTPS with the fixtures' throwaway CA, Range like the providers): a Google-style `alt=media` source
that needs its bearer, and a OneDrive-style pre-authenticated download URL. Only the indexer side talks to the drive.
The transport cases live here too: a drive connection that drops once (ffmpeg reopens through a new OPEN) or keeps
dropping, an untrusted drive certificate and an empty recording (both refused before the sandbox). The media runner's
own cases run at the engine level in `tests/unit/adapters/sandbox_engine/test_media_engine_runner_cases.py`.

Replaced, and only these: the OIDC check of the caller's ID token (no Google offline) and the child launcher's per-uid
process and file limits (RLIMIT_NPROC counts every process of the uid: a developer's own uid runs far more than the
service's engine user; card A's ffmpeg test raises them the same way). Slow: every call starts a Python child and ffmpeg.
"""
import asyncio
import http.server
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig

import sandbox_main
from src.adapters.sandbox_client.media_range_source import HttpRangeSource
from src.adapters.sandbox_client.sandbox_media_adapter import SandboxMediaAdapter
from src.adapters.sandbox_client.sandbox_tunnel_client import SandboxTunnelClient
from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.domain.drive_extraction import ParserUnavailable
from src.domain.drive_media_source import MediaSource
from src.domain.drive_transcript import MediaStatus, MediaUnreadable, WindowSpec
from src.domain.drive_transcript_windows import plan_windows
from src.web import sandbox_caller_gate
from tests.unit.adapters.drive_media_fixtures import (
    HAVE_FFMPEG, make_audio_mp4, make_ca_and_server_cert, make_cueless_webm,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed"),
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"),
                       reason="children run as the invoking user: needs a non-root user"),
]

INDEXER_TOKEN = "tok-indexer-oidc"
GOOGLE_BEARER = "tok-garden-drive"
ONEDRIVE_TEMPAUTH = "tok-memo-download"
MP4_FAMILY = "mov,mp4,m4a,3gp,3g2,mj2"
WEBM = "matroska,webm"
WALL_S = 60.0


# ---- the fake drive -----------------------------------------------------------------------------------------------

class FakeDrive:
    """HTTPS on localhost (the fixtures' throwaway CA) serving recordings with Range. `files` maps a request path (query
    included) to (bytes, the bearer it requires, or None for a pre-authenticated download URL). Every request is
    recorded as (path, Range, Authorization). `drop_at` (an absolute byte offset) cuts a streamed body there and closes
    the connection, the first stream only with `drop_first_only`: a drive connection that dies mid-recording."""

    def __init__(self, directory: Path) -> None:
        self.ca_pem, cert, key = make_ca_and_server_cert(directory)
        self.files: Dict[str, Tuple[bytes, Optional[str]]] = {}
        self.requests: List[Tuple[str, Optional[str], Optional[str]]] = []
        self.drop_at: Optional[int] = None
        self.drop_first_only = False
        self._streams = 0
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                auth = self.headers.get("Authorization")
                rng = self.headers.get("Range")
                owner.requests.append((self.path, rng, auth))
                entry = owner.files.get(self.path)
                if entry is None:
                    self.send_error(404)
                    return
                data, bearer = entry
                if bearer is not None and auth != f"Bearer {bearer}":
                    self.send_error(401)
                    return
                match = re.fullmatch(r"bytes=([0-9]+)-([0-9]*)", rng or "")
                if match is None or int(match.group(1)) >= len(data):
                    self.send_error(416)
                    return
                start = int(match.group(1))
                end = min(int(match.group(2)) if match.group(2) else len(data) - 1, len(data) - 1)
                body = data[start:end + 1]
                cut = False
                if not match.group(2) and owner.drop_at is not None:          # a stream, not the size probe
                    cut = not owner.drop_first_only or owner._streams == 0
                    owner._streams += 1
                self.send_response(206)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    if cut:
                        self.wfile.write(body[:max(0, owner.drop_at - start)])
                        self.wfile.flush()
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                    pass           # the indexer closed a stream the sandbox no longer reads (CLOSE)

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def url(self, path: str) -> str:
        return f"https://127.0.0.1:{self.port}{path}"     # the CA's server cert names 127.0.0.1 too

    def google(self, file_id: str, data: bytes) -> MediaSource:
        """Google's shape: `alt=media` with the user's bearer, which lives at least MEDIA_TOKEN_MIN_LIFE."""
        path = f"/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
        self.files[path] = (data, GOOGLE_BEARER)
        return MediaSource(url=self.url(path), headers={"Authorization": f"Bearer {GOOGLE_BEARER}"},
                           expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    def onedrive(self, item_id: str, data: bytes) -> MediaSource:
        """OneDrive's shape: the Graph `downloadUrl`, pre-authenticated, no header."""
        path = f"/download/{item_id}?tempauth={ONEDRIVE_TEMPAUTH}"
        self.files[path] = (data, None)
        return MediaSource(url=self.url(path))


@pytest.fixture
def drive(tmp_path):
    made = FakeDrive(tmp_path)
    yield made
    made.close()


# ---- the light service ------------------------------------------------------------------------------------------

class Recycler:
    """The supervisor's pipe: counts the instance recycles the route asks for."""

    def __init__(self) -> None:
        self.calls = 0

    def request_served(self) -> None:
        self.calls += 1


class Light:
    def __init__(self, port: int, recycler: Recycler) -> None:
        self.port = port
        self.recycler = recycler


@pytest.fixture
async def light(tmp_path, monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setitem(ChildLauncher.__init__.__kwdefaults__, "nproc", 4000)
    monkeypatch.setitem(ChildLauncher.__init__.__kwdefaults__, "nofile", 256)
    emails = {f"Bearer {INDEXER_TOKEN}": "indexer@example.com", "Bearer tok-main-oidc": "main@example.com"}
    monkeypatch.setattr(sandbox_caller_gate, "verify_oidc_identity", lambda auth, audiences: emails.get(auth))
    work, out = tmp_path / "light-work", tmp_path / "light-out"
    work.mkdir()
    out.mkdir(mode=0o700)
    env = {"PATH": os.environ.get("PATH", ""), "SANDBOX_SERVICE": "light", "SANDBOX_RECYCLE": "abnormal",
           "SANDBOX_URL": "https://sandbox-light.test", "SANDBOX_CALLERS_MAIN": "main@example.com",
           "SANDBOX_CALLERS_INDEXER": "indexer@example.com", "SANDBOX_WORK_ROOT": str(work),
           "SANDBOX_OUTPUT_ROOT": str(out)}
    recycler = Recycler()
    app = sandbox_main.build_app(env, recycler)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    config = HypercornConfig()
    config.bind = [f"fd://{sock.detach()}"]
    config.accesslog = None
    config.graceful_timeout = 0.5
    shutdown = asyncio.Event()
    task = asyncio.ensure_future(serve(app, config, shutdown_trigger=shutdown.wait))
    await asyncio.sleep(0)
    yield Light(port, recycler)
    shutdown.set()
    try:
        await asyncio.wait_for(task, 5)
    except (asyncio.TimeoutError, Exception):
        task.cancel()


def media_port(light: Light, drive: FakeDrive, token: str = INDEXER_TOKEN,
               ca_pem: Optional[str] = None) -> SandboxMediaAdapter:
    """The indexer's port as `build_sandbox_media` builds it, with the ID token and the drive's CA supplied here
    (`ca_pem` trusts another CA instead)."""
    trusted = ssl.create_default_context(cafile=ca_pem or drive.ca_pem)
    # The fixtures' throwaway CA carries no Authority Key Identifier, which Python 3.13's strict mode requires;
    # verification itself (chain, host name) stays on.
    trusted.verify_flags &= ~ssl.VERIFY_X509_STRICT

    async def id_token() -> str:
        return token

    def range_source(source: MediaSource, wall_s: float) -> HttpRangeSource:
        return HttpRangeSource(source, wall_s=wall_s, transport=httpx.AsyncHTTPTransport(verify=trusted))

    return SandboxMediaAdapter(SandboxTunnelClient(f"http://127.0.0.1:{light.port}", id_token),
                               range_source=range_source)


# ---- helpers ----------------------------------------------------------------------------------------------------

def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   stdout=subprocess.DEVNULL)


def _duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         check=True, stdout=subprocess.PIPE).stdout
    return float(out.strip())


def _silent_film(path: Path) -> bytes:
    """A video with no audio stream at all."""
    _ffmpeg("-f", "lavfi", "-i", "color=c=green:s=16x16:r=2:d=4", "-c:v", "libx264", "-preset", "ultrafast", str(path))
    return path.read_bytes()


def _demuxer_file(directory: Path, demuxer: str) -> bytes:
    """Six seconds of tone in each self-contained container the format allowlist admitted (T-R32)."""
    path = directory / f"Coimbra weekend voice memo.{demuxer}"
    sine = ["-f", "lavfi", "-i", "sine=frequency=440:duration=6"]
    if demuxer == "avi":
        _ffmpeg(*sine, "-c:a", "pcm_s16le", str(path))
    elif demuxer == "asf":
        _ffmpeg(*sine, "-c:a", "wmav2", str(path))
    elif demuxer == "aiff":
        _ffmpeg(*sine, str(path))
    elif demuxer == "flv":
        _ffmpeg(*sine, "-c:a", "mp3", str(path))
    else:                                   # AMR-NB: no encoder here; 300 silent 12.2 kbit/s frames = 6 s
        path.write_bytes(b"#!AMR\n" + (bytes([0x3C]) + bytes(31)) * 300)
    return path.read_bytes()


def _mp3_understating(directory: Path) -> bytes:
    """60 s of noise then 60 s of tone, VBR without a Xing header: the container's estimate is well under 120 s."""
    path = directory / "Community garden meeting minutes.mp3"
    _ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=60:a=0.5", "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
            "-filter_complex", "[0][1]concat=n=2:v=0:a=1", "-c:a", "libmp3lame", "-q:a", "5", "-write_xing", "0",
            str(path))
    return path.read_bytes()


def _assert_windows(outcome, out_dir: Path) -> None:
    """Every window is the Opus file the port promises, at `out_dir/window_<index>.opus`, as long as it says."""
    assert sorted(os.path.basename(p) for _, p in outcome.windows) == sorted(p.name for p in out_dir.iterdir())
    for spec, path in outcome.windows:
        assert path == str(out_dir / f"window_{spec.index}.opus") and Path(path).read_bytes()[:4] == b"OggS"
        assert abs(_duration(path) - spec.duration_s) < 0.1


# ---- the contract -----------------------------------------------------------------------------------------------

async def test_a_google_recording_is_probed_and_cut_with_its_bearer_kept_in_the_indexer(tmp_path, light, drive,
                                                                                         caplog):
    local = tmp_path / "Community garden meeting (Alex, Kim, Jordan).mp4"
    data = make_audio_mp4(local, 6)
    assert data.find(b"moov") > data.find(b"mdat")                  # a phone's layout: the index after the media
    source = drive.google("fid-garden-0001", data)
    media = media_port(light, drive)

    with caplog.at_level(logging.DEBUG):
        probed = await media.probe(source)
        assert probed.status is MediaStatus.OK, probed
        probe = probed.probe
        assert probe.has_audio and probe.duration_known and probe.container == MP4_FAMILY
        assert abs(probe.duration_s - _duration(str(local))) <= 0.05

        out = tmp_path / "windows"
        cut = await media.extract_windows(source, [WindowSpec(0, 0.0, 4.0), WindowSpec(1, 3.0, 1800.0)], str(out),
                                          wall_s=WALL_S, container=probe.container)
    assert cut.status is MediaStatus.OK and [s.index for s, _ in cut.windows] == [0, 1]
    assert cut.eof and abs(cut.duration_s - 6.0) < 0.5 and not cut.reached_bound
    assert abs(cut.windows[0][0].duration_s - 4.0) < 0.1 and abs(cut.windows[1][0].duration_s - 3.0) < 0.5
    _assert_windows(cut, out)

    # the drive saw only the indexer: every read carried the bearer, the size first, then the tunnel's offsets
    assert drive.requests and all(auth == f"Bearer {GOOGLE_BEARER}" for _, _, auth in drive.requests)
    assert drive.requests[0][1] == "bytes=0-0"
    offsets = [int(rng[6:].split("-")[0]) for _, rng, _ in drive.requests if rng and not rng.endswith("-0")]
    assert any(offset > 0 for offset in offsets)                     # a seek to the moov, through an OPEN
    # httpx/httpcore print each request's URL at INFO; every entry point quiets them (`quiet_third_party_loggers`)
    ours = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore")))
    assert ours and GOOGLE_BEARER not in ours and "alt=media" not in ours and "fid-garden" not in ours
    assert light.recycler.calls == 0                                 # RESULT ok both times: no recycle


async def test_a_onedrive_voice_memo_without_cues_is_read_in_one_pass(tmp_path, light, drive):
    data = make_cueless_webm(6)                                      # a browser recorder's webm: no Duration, no Cues
    source = drive.onedrive("fid-memo-0002", data)
    media = media_port(light, drive)

    probed = await media.probe(source)
    assert probed.status is MediaStatus.OK, probed
    probe = probed.probe
    assert probe.has_audio and not probe.duration_known and probe.duration_s == 0.0
    assert "webm" in probe.container.split(",")

    out = tmp_path / "windows"
    cut = await media.extract_windows(source, plan_windows(14_400)[:3], str(out), wall_s=WALL_S,
                                      container=probe.container, duration_known=False)
    assert cut.status is MediaStatus.OK and cut.eof and not cut.reached_bound
    assert abs(cut.duration_s - 6.0) < 0.5
    ((spec, _),) = cut.windows                                       # the others begin past the end of the file
    assert spec.index == 0 and spec.start_s == 0.0 and abs(spec.duration_s - 6.0) < 0.5
    _assert_windows(cut, out)
    assert all(auth is None for _, _, auth in drive.requests)        # pre-authenticated: no header ever sent
    assert {path for path, _, _ in drive.requests} == {f"/download/fid-memo-0002?tempauth={ONEDRIVE_TEMPAUTH}"}
    assert light.recycler.calls == 0


async def test_a_video_without_audio_probes_as_such(tmp_path, light, drive):
    source = drive.google("fid-walkthrough-0003", _silent_film(tmp_path / "Community garden walkthrough.mp4"))
    probed = await media_port(light, drive).probe(source)
    assert probed.status is MediaStatus.OK and probed.probe.has_audio is False
    assert probed.probe.container == MP4_FAMILY


@pytest.mark.parametrize("demuxer", ["avi", "asf", "aiff", "amr", "flv"])
async def test_each_admitted_self_contained_container_probes_and_yields_one_window(tmp_path, light, drive, demuxer):
    source = drive.onedrive(f"fid-memo-{demuxer}", _demuxer_file(tmp_path, demuxer))
    media = media_port(light, drive)
    probed = await media.probe(source)
    assert probed.status is MediaStatus.OK, probed
    probe = probed.probe
    assert probe.has_audio and abs(probe.duration_s - 6.0) < 1.0 and demuxer in probe.container.split(",")
    out = tmp_path / "windows"
    cut = await media.extract_windows(source, [WindowSpec(0, 0.0, probe.duration_s)], str(out), wall_s=WALL_S,
                                      container=probe.container)
    assert cut.status is MediaStatus.OK and len(cut.windows) == 1
    _assert_windows(cut, out)


async def test_a_header_that_understates_the_audio_does_not_cut_the_last_window_short(tmp_path, light, drive):
    source = drive.onedrive("fid-minutes-0004", _mp3_understating(tmp_path))
    media = media_port(light, drive)
    probe = (await media.probe(source)).probe
    if probe.duration_s > 110:
        pytest.skip("this ffmpeg reads the duration exactly")
    out = tmp_path / "windows"
    cut = await media.extract_windows(source, [WindowSpec(0, 0.0, probe.duration_s)], str(out), wall_s=WALL_S,
                                      container=probe.container)
    assert cut.status is MediaStatus.OK and cut.eof and abs(cut.duration_s - 120.0) < 3.0
    ((spec, _),) = cut.windows
    assert abs(spec.duration_s - 120.0) < 3.0


async def test_a_playlist_is_refused_nothing_it_names_is_fetched_and_the_instance_recycles(tmp_path, light, drive):
    named = drive.url("/download/fid-elsewhere?tempauth=tok-other")
    drive.files["/download/fid-elsewhere?tempauth=tok-other"] = (b"\0" * 64, None)
    playlist = f"#EXTM3U\n#EXTINF:10,\n{named}\n#EXTINF:10,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n".encode()
    source = drive.onedrive("fid-list-0005", playlist)
    with pytest.raises(MediaUnreadable):
        await media_port(light, drive).probe(source)
    assert {path for path, _, _ in drive.requests} == {f"/download/fid-list-0005?tempauth={ONEDRIVE_TEMPAUTH}"}
    assert light.recycler.calls == 1                                 # any end but RESULT ok recycles (abnormal)


async def test_a_caller_outside_the_indexer_group_cannot_open_a_tunnel(tmp_path, light, drive):
    """The light service's MAIN group parses only (`INDEXER += media`): its token is refused before any stream, so the
    drive is read for the size only and never through the tunnel."""
    source = drive.onedrive("fid-memo-0006", make_cueless_webm(2))
    with pytest.raises(ParserUnavailable):
        await media_port(light, drive, token="tok-main-oidc").probe(source)
    assert [rng for _, rng, _ in drive.requests] == ["bytes=0-0"]
    assert light.recycler.calls == 0


# ---- the transport: a drive connection that drops, an untrusted or empty source -----------------------------------

async def test_a_dropped_drive_connection_is_reopened_and_the_pass_reads_the_whole_file(tmp_path, light, drive):
    """The drive connection dies at 30 %: the indexer ends that stream (END error), the child's loopback server closes
    ffmpeg's socket, ffmpeg reconnects with a Range read (a new OPEN), so the pass sees the real end (N1)."""
    data = make_cueless_webm(300)
    source = drive.onedrive("fid-memo-0007", data)
    drive.drop_at, drive.drop_first_only = int(len(data) * 0.3), True
    out = tmp_path / "windows"
    cut = await media_port(light, drive).extract_windows(source, plan_windows(14_400)[:3], str(out), wall_s=WALL_S,
                                                         container=WEBM, duration_known=False)
    assert cut.status is MediaStatus.OK and cut.eof and abs(cut.duration_s - 300.0) < 2.0
    streams = [rng for _, rng, _ in drive.requests if rng and rng.endswith("-") and rng != "bytes=0-"]
    assert streams, drive.requests                                   # reopened past the start
    _assert_windows(cut, out)


async def test_a_drive_connection_that_keeps_dropping_is_an_error_never_an_eof(tmp_path, light, drive):
    data = make_cueless_webm(300)
    source = drive.onedrive("fid-memo-0008", data)
    drive.drop_at = int(len(data) * 0.3)
    out = tmp_path / "windows"
    with pytest.raises(MediaUnreadable):
        await media_port(light, drive).extract_windows(source, plan_windows(14_400)[:3], str(out), wall_s=WALL_S,
                                                       container=WEBM, duration_known=False)
    assert not out.exists() or not list(out.glob("window_*"))


async def test_a_known_duration_window_cut_short_by_a_drop_is_not_reported_at_its_planned_length(tmp_path, light,
                                                                                                drive):
    """Per-window runs rely on `-xerror` and measure the produced window against the plan (N1)."""
    local = tmp_path / "Coimbra weekend voice memo.webm"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=100", "-c:a", "libopus", str(local))   # Duration + Cues
    data = local.read_bytes()
    source = drive.onedrive("fid-memo-0009", data)
    drive.drop_at = int(len(data) * 0.5)
    out = tmp_path / "windows"
    with pytest.raises(MediaUnreadable):
        await media_port(light, drive).extract_windows(source, [WindowSpec(0, 0.0, 90.0)], str(out), wall_s=WALL_S,
                                                       container=WEBM)
    assert not out.exists() or not list(out.glob("window_*"))


async def test_a_drive_whose_certificate_is_not_trusted_is_refused_before_the_sandbox(tmp_path, light, drive):
    """TLS to the drive is the indexer's now (the sandbox reads loopback http only): a server whose CA is not the
    trusted one is refused at the size read, and the sandbox is never called."""
    (tmp_path / "other-ca").mkdir()
    other = FakeDrive(tmp_path / "other-ca")
    try:
        source = drive.onedrive("fid-memo-0010", make_cueless_webm(2))
        with pytest.raises(MediaUnreadable):
            await media_port(light, drive, ca_pem=other.ca_pem).probe(source)
    finally:
        other.close()
    assert drive.requests == [] and light.recycler.calls == 0


async def test_an_empty_recording_is_refused_before_the_sandbox(tmp_path, light, drive):
    source = drive.onedrive("fid-memo-0011", b"")
    with pytest.raises(MediaUnreadable):
        await media_port(light, drive).probe(source)
    assert [rng for _, rng, _ in drive.requests] == ["bytes=0-0"] and light.recycler.calls == 0
