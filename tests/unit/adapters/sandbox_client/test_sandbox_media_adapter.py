"""SandboxMediaAdapter (MediaAudioPort) end to end over a scripted loopback HTTP/2 sandbox (S5 design §2.3, §2.6; card
C): result mapping, retries and outages, the hostile server ending as `tunnel_violation`, the port's wall, and no drive
URL, token or file identity in anything the indexer writes to the sandbox."""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator, List

import h2.errors
import httpx
import pytest

from src.adapters.sandbox_client.media_range_source import HttpRangeSource
from src.adapters.sandbox_client.sandbox_media_adapter import PROBE_WALL_S, SandboxMediaAdapter
from src.adapters.sandbox_client.sandbox_tunnel_client import SandboxTunnelClient
from src.domain import sandbox_contract as sc
from src.domain.drive_extraction import ParserUnavailable
from src.domain.drive_media_source import MediaSource
from src.domain.drive_transcript import MediaStatus, MediaUnreadable, WindowSpec
from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from src.domain.sandbox_tunnel import COLLECT_MARGIN_S, FrameType, MAX_OPENS, tunnel_budget
from tests.unit.adapters.sandbox_client.test_sandbox_media_range_source import Chunks
from tests.unit.adapters.sandbox_client.tunnel_h2_fake import (
    FakeTunnelServer,
    Peer,
    failure_result,
    outcome,
    probe_result,
    windows_result,
)

ID_TOKEN = "id-tok-sandbox-audience"
DRIVE_TOKEN = "tok-drive-coimbra-91X"
DRIVE_URL = "https://www.example.com/drive/v3/files/rec-coimbra-memo-0007?alt=media"
FILE_NAME = "Coimbra weekend voice memo.m4a"
RECORDING = bytes((i * 13 + 5) % 253 for i in range(200_000))
CONTAINER = "mov,mp4,m4a,3gp,3g2,mj2"
PLAN = [WindowSpec(index=0, start_s=0.0, duration_s=1860.0), WindowSpec(index=1, start_s=1800.0, duration_s=1860.0)]
CANCEL = int(h2.errors.ErrorCodes.CANCEL)


async def id_token() -> str:
    return ID_TOKEN


def drive_source() -> MediaSource:
    return MediaSource(url=DRIVE_URL, headers={"Authorization": f"Bearer {DRIVE_TOKEN}"})


class FakeRange:
    """The drive side without HTTP: the recording in memory."""

    def __init__(self, data: bytes = RECORDING) -> None:
        self.data = data
        self.offsets: List[int] = []
        self.closed = False

    async def size(self) -> int:
        return len(self.data)

    async def stream(self, offset: int) -> AsyncIterator[bytes]:
        self.offsets.append(offset)
        for at in range(offset, len(self.data), 32 * 1024):
            yield self.data[at:at + 32 * 1024]

    async def aclose(self) -> None:
        self.closed = True


def adapter(url: str, *, ranges: List[FakeRange] = None, **client_kw) -> SandboxMediaAdapter:
    made = ranges if ranges is not None else []

    def factory(source: MediaSource, wall_s: float) -> FakeRange:
        made.append(FakeRange())
        return made[-1]

    return SandboxMediaAdapter(SandboxTunnelClient(url, id_token, **client_kw), range_source=factory)


async def probing(peer: Peer) -> None:
    start = await peer.start(SandboxOp.MEDIA_PROBE)
    await peer.respond()
    await peer.serve_stream(1, 0, start.source_bytes)
    await peer.frame(FrameType.RESULT, payload=probe_result(start), end=True)


def windows_script(indexes=(0, 1), status=SandboxStatus.OK, eof=False, duration_s=0.0, detail=""):
    async def script(peer: Peer) -> None:
        start = await peer.start(SandboxOp.MEDIA_AUDIO_WINDOWS)
        await peer.respond()
        await peer.serve_stream(1, 0, start.source_bytes)
        for i in indexes:
            await peer.frame(FrameType.WINDOW, i, payload=f"opus-window-{i}".encode() * 100)
        await peer.frame(FrameType.RESULT, payload=windows_result(start, list(indexes), status=status, eof=eof,
                                                                  duration_s=duration_s, detail=detail), end=True)
    return script


def refusing(status: int, header=None):
    async def script(peer: Peer) -> None:
        await peer.respond(status, header, end=True)
    return script


def resetting():
    async def script(peer: Peer) -> None:
        await peer.next_frame()
        await peer.respond()
        await peer.frame(FrameType.OPEN, 1, 0)
        await peer.next_frame()
        peer.reset()
    return script


def result_only(status: SandboxStatus, detail: str = ""):
    async def script(peer: Peer) -> None:
        start = await peer.start(SandboxOp.MEDIA_AUDIO_WINDOWS)
        await peer.respond()
        await peer.frame(FrameType.RESULT, payload=failure_result(start, status, detail), end=True)
    return script


async def extract(sandbox: SandboxMediaAdapter, out_dir: str, *, wall_s: float = 240.0, windows=PLAN,
                  duration_known: bool = True):
    return await sandbox.extract_windows(drive_source(), windows, out_dir, wall_s=wall_s, container=CONTAINER,
                                         duration_known=duration_known)


class TestOutcomes:
    async def test_probe(self):
        ranges: List[FakeRange] = []
        async with FakeTunnelServer(probing) as server:
            outcome_ = await adapter(server.url, ranges=ranges).probe(drive_source())
        assert outcome_.status is MediaStatus.OK
        assert (outcome_.probe.duration_s, outcome_.probe.has_audio, outcome_.probe.container,
                outcome_.probe.duration_known) == (1834.5, True, CONTAINER, True)
        assert ranges[0].offsets == [0] and ranges[0].closed

    async def test_windows_are_written_to_out_dir_with_their_measured_lengths(self, tmp_path):
        async with FakeTunnelServer(windows_script()) as server:
            result = await extract(adapter(server.url), str(tmp_path / "out"))
        assert result.status is MediaStatus.OK and not result.eof
        assert [(spec.index, spec.start_s, spec.duration_s) for spec, _ in result.windows] == [
            (0, 0.0, 1859.5), (1, 1800.0, 1859.5)]
        for spec, path in result.windows:
            assert path == str(tmp_path / "out" / f"window_{spec.index}.opus")
            with open(path, "rb") as handle:
                assert handle.read() == f"opus-window-{spec.index}".encode() * 100

    async def test_a_pass_reports_eof_and_the_real_duration(self, tmp_path):
        script = windows_script(indexes=(0,), eof=True, duration_s=1203.25)
        async with FakeTunnelServer(script) as server:
            result = await extract(adapter(server.url), str(tmp_path), windows=PLAN[:1], duration_known=False)
        assert result.eof and result.duration_s == 1203.25 and len(result.windows) == 1

    async def test_a_wall_hit_inside_the_run_keeps_the_windows_finished_before_it(self, tmp_path):
        script = windows_script(indexes=(0,), status=SandboxStatus.TIMEOUT, detail="wall after 1 windows")
        async with FakeTunnelServer(script) as server:
            result = await extract(adapter(server.url), str(tmp_path))
        assert result.status is MediaStatus.TIMEOUT and [s.index for s, _ in result.windows] == [0]
        assert os.listdir(tmp_path) == ["window_0.opus"]

    @pytest.mark.parametrize("status, expected", [(SandboxStatus.TIMEOUT, MediaStatus.TIMEOUT),
                                                  (SandboxStatus.MEMORY, MediaStatus.MEMORY)])
    async def test_a_killed_run_is_an_outcome_without_windows(self, tmp_path, status, expected):
        async with FakeTunnelServer(result_only(status, "killed")) as server:
            result = await extract(adapter(server.url), str(tmp_path))
        assert result.status is expected and result.windows == () and result.detail == "killed"

    @pytest.mark.parametrize("status", [SandboxStatus.ERROR, SandboxStatus.TOO_LARGE])
    async def test_an_unreadable_file_is_media_unreadable(self, tmp_path, status):
        async with FakeTunnelServer(result_only(status, "moov atom not found")) as server:
            with pytest.raises(MediaUnreadable) as err:
                await extract(adapter(server.url), str(tmp_path))
        assert err.value.detail == "moov atom not found"

    async def test_a_bad_request_verdict_is_ours_not_the_files(self, tmp_path):
        async with FakeTunnelServer(result_only(SandboxStatus.BAD_REQUEST)) as server:
            with pytest.raises(ParserUnavailable) as err:
                await extract(adapter(server.url), str(tmp_path))
        assert str(err.value) == "misconfigured"

    async def test_inputs_the_port_refuses_before_any_call(self, tmp_path):
        sandbox = adapter("http://127.0.0.1:9")
        with pytest.raises(MediaUnreadable):
            await sandbox.extract_windows(drive_source(), PLAN, str(tmp_path), wall_s=240, container="")
        with pytest.raises(MediaUnreadable):
            await sandbox.extract_windows(drive_source(), PLAN, str(tmp_path), wall_s=240, container="mp4;rm")
        with pytest.raises(ValueError):
            await extract(sandbox, str(tmp_path), windows=[PLAN[0], WindowSpec(5, 9000.0, 1860.0)],
                          duration_known=False)
        empty = await extract(sandbox, str(tmp_path), windows=[])
        assert empty.status is MediaStatus.OK and empty.windows == ()


class TestRetriesAndOutages:
    async def test_lost_once_is_retried_on_a_fresh_connection(self):
        async with FakeTunnelServer(resetting(), probing) as server:
            result = await adapter(server.url).probe(drive_source())
        assert result.status is MediaStatus.OK and len(server.peers) == 2

    async def test_lost_twice_is_sandbox_lost(self):
        async with FakeTunnelServer(resetting()) as server:
            with pytest.raises(MediaUnreadable) as err:
                await adapter(server.url).probe(drive_source())
        assert err.value.detail == "sandbox_lost" and len(server.peers) == 2

    async def test_lost_with_too_little_wall_left_is_systemic(self, tmp_path):
        async with FakeTunnelServer(resetting()) as server:
            with pytest.raises(ParserUnavailable) as err:
                await extract(adapter(server.url), str(tmp_path), wall_s=60.0)    # < TRANSCRIBE_MIN_RUN_WALL_S left
        assert str(err.value) == "unavailable" and err.value.charges_file is False and len(server.peers) == 1

    @pytest.mark.parametrize("scripts, reason, calls", [
        ((refusing(401),), "misconfigured", 1),
        ((refusing(403),), "misconfigured", 1),                   # no IAM-propagation retry (design §2.3, §5.1)
        ((refusing(404),), "misconfigured", 1),
        ((refusing(400, outcome(SandboxStatus.BAD_REQUEST, "bad_start")),), "misconfigured", 1),
        ((refusing(429),), "busy", 1),
        ((refusing(503, outcome(SandboxStatus.ERROR)),), "busy", 1),
        ((refusing(503, outcome(SandboxStatus.UNCONFIGURED)),), "unavailable", 1),
        ((refusing(503, outcome(SandboxStatus.RECYCLING)),), "busy", 2),
        ((refusing(503),), "unavailable", 2),
    ], ids=["401", "403", "404", "400-bad-request", "429", "503-busy", "503-unconfigured", "503-recycling-twice",
            "503-no-header-twice"])
    async def test_answers_before_the_stream(self, scripts, reason, calls):
        async with FakeTunnelServer(*scripts) as server:
            with pytest.raises(ParserUnavailable) as err:
                await adapter(server.url).probe(drive_source())
        assert str(err.value) == reason and len(server.peers) == calls

    async def test_recycling_once_is_retried(self):
        async with FakeTunnelServer(refusing(503, outcome(SandboxStatus.RECYCLING)), probing) as server:
            result = await adapter(server.url).probe(drive_source())
        assert result.status is MediaStatus.OK and len(server.peers) == 2

    async def test_a_5xx_without_a_verdict_twice_is_sandbox_lost(self):
        async with FakeTunnelServer(refusing(500)) as server:
            with pytest.raises(MediaUnreadable) as err:
                await adapter(server.url).probe(drive_source())
        assert err.value.detail == "sandbox_lost"

    async def test_an_unreachable_sandbox_is_parser_unavailable(self):
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        with pytest.raises(ParserUnavailable) as err:
            await adapter(f"http://127.0.0.1:{port}").probe(drive_source())
        assert str(err.value) == "unavailable" and err.value.charges_file is False

    @pytest.mark.parametrize("failure, reason", [
        (SandboxUnavailable("metadata blip"), "unavailable"),
        (SandboxMisconfigured("no credentials"), "misconfigured"),
        (RuntimeError("bug"), "misconfigured"),
    ])
    async def test_no_id_token_is_parser_unavailable(self, failure, reason):
        async def token() -> str:
            raise failure

        sandbox = SandboxMediaAdapter(SandboxTunnelClient("http://127.0.0.1:9", token),
                                      range_source=lambda s, w: FakeRange())
        with pytest.raises(ParserUnavailable) as err:
            await sandbox.probe(drive_source())
        assert str(err.value) == reason


async def _open_beyond(peer, start):
    await peer.frame(FrameType.OPEN, 1, start.source_bytes)


async def _too_many_opens(peer, start):
    for i in range(1, MAX_OPENS + 2):
        await peer.frame(FrameType.OPEN, i, 0)
        await peer.frame(FrameType.CLOSE, i)


async def _window_not_requested(peer, start):
    await peer.frame(FrameType.WINDOW, 9, payload=b"opus")


async def _window_too_large(peer, start):
    await peer.header_only(int(FrameType.WINDOW), 0, 0, sc.CEILING_FILE_BYTES + 1)


async def _two_results(peer, start):
    await peer.frame(FrameType.RESULT, payload=windows_result(start, []))
    await peer.frame(FrameType.RESULT, payload=windows_result(start, []), end=True)


async def _frame_after_result(peer, start):
    await peer.frame(FrameType.RESULT, payload=windows_result(start, []))
    await peer.frame(FrameType.OPEN, 1, 0, end=True)


async def _malformed_result(peer, start):
    await peer.frame(FrameType.RESULT, payload=b'{"status":"ok","detail":""}', end=True)


HOSTILE = [_open_beyond, _too_many_opens, _window_not_requested, _window_too_large, _two_results,
           _frame_after_result, _malformed_result]


class TestHostileSandbox:
    @pytest.mark.parametrize("tail", HOSTILE, ids=[f.__name__.lstrip("_") for f in HOSTILE])
    async def test_a_breach_is_tunnel_violation_charged_to_the_file_and_never_retried(self, tmp_path, tail):
        async def script(peer: Peer) -> None:
            start = await peer.start(SandboxOp.MEDIA_AUDIO_WINDOWS)
            await peer.respond()
            await tail(peer, start)
            await peer.wait_gone(3)

        loop = asyncio.get_running_loop()
        async with FakeTunnelServer(script) as server:
            began = loop.time()
            with pytest.raises(MediaUnreadable) as err:
                await extract(adapter(server.url), str(tmp_path))
            took = loop.time() - began
            await server.peers[0].wait_gone(3)
        assert err.value.detail == "tunnel_violation"
        assert server.peers[0].client_reset == CANCEL and len(server.peers) == 1
        assert took <= 1.0 + 0.5 * (tail is _too_many_opens)     # 514 frames take their own time to send
        assert os.listdir(tmp_path) == []


class TestTheWall:
    async def test_start_wall_leaves_the_collect_margin(self):
        seen = {}

        async def script(peer: Peer) -> None:
            start = await peer.start(SandboxOp.MEDIA_PROBE)
            seen["wall"], seen["budget"] = start.wall_s, start.budget_bytes
            await peer.respond()
            await peer.frame(FrameType.RESULT, payload=probe_result(start), end=True)

        async with FakeTunnelServer(script) as server:
            await adapter(server.url).probe(drive_source())
        assert PROBE_WALL_S - COLLECT_MARGIN_S - 0.5 <= seen["wall"] <= PROBE_WALL_S - COLLECT_MARGIN_S
        assert seen["budget"] == tunnel_budget(len(RECORDING))

    async def test_the_call_never_exceeds_the_ports_wall(self, tmp_path):
        async def beating(peer: Peer) -> None:
            await peer.next_frame()
            await peer.respond()
            while not peer.gone.is_set():
                await asyncio.sleep(0.05)
                await peer.frame(FrameType.BEAT)

        loop = asyncio.get_running_loop()
        async with FakeTunnelServer(beating) as server:
            began = loop.time()
            with pytest.raises(ParserUnavailable) as err:
                await extract(adapter(server.url), str(tmp_path), wall_s=0.6)
            took = loop.time() - began
        assert took <= 0.6 + 0.25 and str(err.value) == "unavailable"
        assert len(server.peers) == 1


class TestNothingOfTheRecordingReachesTheSandbox:
    async def test_every_byte_written_to_the_sandbox_is_free_of_the_drive_url_token_and_identity(self, tmp_path):
        def upstream(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == f"Bearer {DRIVE_TOKEN}" and str(request.url) == DRIVE_URL
            first, _, last = request.headers["Range"].removeprefix("bytes=").partition("-")
            end = int(last) if last else len(RECORDING) - 1
            body = RECORDING[int(first):end + 1]
            return httpx.Response(206, stream=Chunks(body), headers={
                "Content-Range": f"bytes {first}-{end}/{len(RECORDING)}"})

        def real_range(source: MediaSource, wall_s: float) -> HttpRangeSource:
            return HttpRangeSource(source, wall_s=wall_s, transport=httpx.MockTransport(upstream))

        async with FakeTunnelServer(windows_script()) as server:
            sandbox = SandboxMediaAdapter(SandboxTunnelClient(server.url, id_token), range_source=real_range)
            result = await extract(sandbox, str(tmp_path))
        assert result.status is MediaStatus.OK
        peer = server.peers[0]
        decoded_headers = " ".join(f"{k}: {v}" for k, v in peer.headers.items())
        assert f"Bearer {ID_TOKEN}" in decoded_headers           # the check can see a token when one is there
        assert RECORDING[:4096] in bytes(peer.body)              # and the body it reads is the real body
        forbidden = [DRIVE_TOKEN, DRIVE_URL, "www.example.com", "alt=media", "rec-coimbra-memo-0007", FILE_NAME,
                     "drive/v3"]
        for text in forbidden:
            needle = text.encode()
            assert needle not in bytes(peer.raw_in), text
            assert text not in decoded_headers, text
            assert needle not in bytes(peer.body), text
