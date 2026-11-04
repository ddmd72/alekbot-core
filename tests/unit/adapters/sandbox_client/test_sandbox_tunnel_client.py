"""SandboxTunnelClient against a scripted loopback HTTP/2 server (S5 design §2.3, §2.6; card C): full duplex on one
stream, the client's authoritative bounds against a hostile server, deadlines, and what the client writes to the wire."""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import AsyncIterator, List, Tuple

import h2.errors
import pytest

from src.adapters.sandbox_client.sandbox_tunnel_client import SandboxTunnelClient, TunnelLost
from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_errors import SandboxMisconfigured
from src.domain.sandbox_tunnel import (
    DATA_MAX,
    END_EOF,
    END_UPSTREAM_ERROR,
    IDLE_S,
    MAX_OPENS,
    OPEN_SLACK_BYTES,
    SEND_STALL_S,
    FrameType,
    TunnelStart,
    TunnelViolation,
    TunnelWindow,
    start_keys,
    tunnel_budget,
)
from tests.unit.adapters.sandbox_client.tunnel_h2_fake import (
    FakeTunnelServer,
    Peer,
    TUNNEL_OUTCOME,
    probe_result,
    windows_result,
)

ID_TOKEN = "id-tok-sandbox-audience"
CANCEL = int(h2.errors.ErrorCodes.CANCEL)
SOURCE = bytes((i * 7 + 3) % 251 for i in range(300_000))     # a stand-in recording, not media


async def id_token() -> str:
    return ID_TOKEN


def probe_start(size: int = len(SOURCE), budget: int = 0, wall_s: float = 45.0) -> TunnelStart:
    return TunnelStart(op=SandboxOp.MEDIA_PROBE, source_bytes=size, budget_bytes=budget or tunnel_budget(size),
                       wall_s=wall_s)


def windows_start(indexes=(0, 1), size: int = len(SOURCE)) -> TunnelStart:
    return TunnelStart(op=SandboxOp.MEDIA_AUDIO_WINDOWS, source_bytes=size, budget_bytes=tunnel_budget(size),
                       wall_s=225.0, container="mov,mp4,m4a,3gp,3g2,mj2", duration_known=True,
                       windows=tuple(TunnelWindow(index=i, start_s=1800.0 * i, duration_s=1860.0) for i in indexes))


class MemoryRange:
    """The upstream: the source in fixed chunks, each OPEN's offset recorded."""

    def __init__(self, data: bytes = SOURCE, chunk: int = 16 * 1024, delay: float = 0.0) -> None:
        self.data = data
        self.chunk = chunk
        self.delay = delay
        self.offsets: List[int] = []
        self.closed = 0

    async def __call__(self, offset: int) -> AsyncIterator[bytes]:
        self.offsets.append(offset)
        try:
            for at in range(offset, len(self.data), self.chunk):
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield self.data[at:at + self.chunk]
        finally:
            self.closed += 1


def client(url: str, **kw) -> SandboxTunnelClient:
    return SandboxTunnelClient(url, id_token, **kw)


async def run(url: str, start: TunnelStart, upstream=None, *, wall: float = 10.0, **kw):
    loop = asyncio.get_running_loop()
    return await client(url, **kw).run(start.op, start, upstream or MemoryRange(), loop.time() + wall)


class TestFullDuplex:
    async def test_probe_answers_opens_on_the_open_request_and_returns_the_verdict(self):
        seen = {}

        async def script(peer: Peer) -> None:
            start = await peer.start(SandboxOp.MEDIA_PROBE)
            await peer.respond()
            seen["first"] = await peer.serve_stream(1, 0, start.source_bytes)
            seen["tail"] = await peer.serve_stream(2, 250_000, start.source_bytes)
            seen["ended_before_result"] = peer.client_ended.is_set()
            await peer.frame(FrameType.RESULT, payload=probe_result(start), end=True)
            await asyncio.wait_for(peer.client_ended.wait(), timeout=5)

        async with FakeTunnelServer(script) as server:
            reply = await run(server.url, probe_start())
        assert server.errors == []
        assert seen["first"] == SOURCE and seen["tail"] == SOURCE[250_000:]
        assert seen["ended_before_result"] is False          # both directions open at once: the body ends after RESULT
        assert reply.result.status is SandboxStatus.OK and reply.result.media.duration_s == 1834.5
        assert reply.windows == () and reply.opens == 2 and reply.bytes_in == len(SOURCE) + 50_000
        assert server.peers[0].client_reset is None

    async def test_the_request_is_headers_meta_and_a_closed_start(self):
        async def script(peer: Peer) -> None:
            start = await peer.start(SandboxOp.MEDIA_AUDIO_WINDOWS)
            await peer.respond()
            await peer.frame(FrameType.WINDOW, 0, payload=b"opus-0")
            await peer.frame(FrameType.RESULT, payload=windows_result(start, [0]), end=True)

        start = windows_start()
        async with FakeTunnelServer(script) as server:
            await run(server.url, start)
        peer = server.peers[0]
        h = peer.headers
        assert (h[":method"], h[":path"], h["authorization"]) == ("POST", "/v1/media.audio_windows", f"Bearer {ID_TOKEN}")
        meta = sc.decode_meta(h[sc.META_HEADER.lower()])
        assert (meta.kind, meta.ext, meta.windows) == (None, None, None)
        assert meta.limits.wall_s == start.wall_s
        first = peer.body_frames[0]
        assert first.kind is FrameType.START and peer.body_violation is None
        assert set(json.loads(first.payload)) == start_keys(SandboxOp.MEDIA_AUDIO_WINDOWS)

    async def test_windows_come_back_in_result_order(self):
        async def script(peer: Peer) -> None:
            start = await peer.start(SandboxOp.MEDIA_AUDIO_WINDOWS)
            await peer.respond()
            await peer.frame(FrameType.WINDOW, 1, payload=b"opus-1")
            await peer.frame(FrameType.WINDOW, 0, payload=b"opus-0" * 1000)
            await peer.frame(FrameType.RESULT, payload=windows_result(start, [0, 1]), end=True)

        async with FakeTunnelServer(script) as server:
            reply = await run(server.url, windows_start())
        assert reply.windows == ((0, b"opus-0" * 1000), (1, b"opus-1"))

    async def test_only_the_newest_stream_is_fed_until_the_older_one_closes(self):
        order: List[Tuple[int, int]] = []

        async def script(peer: Peer) -> None:
            start = await peer.start(SandboxOp.MEDIA_PROBE)
            await peer.respond()
            await peer.frame(FrameType.OPEN, 1, 0)
            first = await peer.next_frame()
            order.append((first.id, first.value))
            await peer.frame(FrameType.OPEN, 2, 200_000)                 # ffmpeg's seek: new before old is closed
            tail = bytearray()
            while True:
                frame = await peer.next_frame()
                order.append((frame.id, int(frame.kind)))
                if frame.kind is FrameType.DATA and frame.id == 2:
                    tail += frame.payload
                if frame.kind is FrameType.END and frame.id == 2:
                    break
            await peer.frame(FrameType.CLOSE, 2)
            await peer.frame(FrameType.CLOSE, 1)
            await peer.frame(FrameType.RESULT, payload=probe_result(start), end=True)
            order.append(("tail", bytes(tail) == SOURCE[200_000:]))

        async with FakeTunnelServer(script) as server:
            await run(server.url, probe_start(), MemoryRange(delay=0.002))
        assert server.errors == []
        assert order[-1] == ("tail", True)
        first_two = next(i for i, item in enumerate(order) if item[0] == 2)
        assert all(item[0] != 1 for item in order[first_two:-1])        # stream 1 starves once 2 exists

    async def test_an_upstream_failure_ends_that_stream_with_end_error(self):
        class Failing(MemoryRange):
            async def __call__(self, offset):
                self.offsets.append(offset)
                yield self.data[offset:offset + 1000]
                raise OSError("upstream dropped")

        got = {}

        async def script(peer: Peer) -> None:
            start = await peer.start(SandboxOp.MEDIA_PROBE)
            await peer.respond()
            await peer.frame(FrameType.OPEN, 1, 0)
            data = bytearray()
            while True:
                frame = await peer.next_frame()
                if frame.kind is FrameType.DATA:
                    data += frame.payload
                if frame.kind is FrameType.END:
                    got["end"] = frame.value
                    break
            got["data"] = bytes(data)
            await peer.frame(FrameType.CLOSE, 1)
            got["reconnect"] = await peer.serve_stream(2, 1000, start.source_bytes)   # ffmpeg reconnects
            await peer.frame(FrameType.RESULT, payload=probe_result(start), end=True)

        upstream = Failing()
        async with FakeTunnelServer(script) as server:
            reply = await run(server.url, probe_start(), upstream)
        assert reply.result.status is SandboxStatus.OK and server.errors == []
        assert got["end"] == END_UPSTREAM_ERROR and got["data"] == SOURCE[:1000]
        assert got["reconnect"] == SOURCE[1000:2000] and upstream.offsets == [0, 1000]

    async def test_the_source_end_is_end_eof_and_data_is_cut_to_data_max(self):
        frames = []

        async def script(peer: Peer) -> None:
            start = await peer.start(SandboxOp.MEDIA_PROBE)
            await peer.respond()
            await peer.frame(FrameType.OPEN, 1, 0)
            while True:
                frame = await peer.next_frame()
                frames.append(frame)
                if frame.kind is FrameType.END:
                    break
            await peer.frame(FrameType.CLOSE, 1)
            await peer.frame(FrameType.RESULT, payload=probe_result(start), end=True)

        async with FakeTunnelServer(script) as server:
            await run(server.url, probe_start(), MemoryRange(chunk=DATA_MAX * 2))
        assert frames[-1].kind is FrameType.END and frames[-1].value == END_EOF
        assert max(len(f.payload) for f in frames[:-1]) == DATA_MAX
        assert b"".join(f.payload for f in frames[:-1]) == SOURCE


async def _violating(script_tail, start: TunnelStart, upstream=None):
    """Runs a server that opens the stream and then does `script_tail`; returns (error, peer, seconds from the last
    server write to the client's reset)."""
    marks = {}

    async def script(peer: Peer) -> None:
        await peer.next_frame()
        await peer.respond()
        await script_tail(peer, start)
        marks["sent"] = asyncio.get_running_loop().time()
        await peer.wait_gone(3)

    async with FakeTunnelServer(script) as server:
        with pytest.raises(TunnelViolation) as err:
            await run(server.url, start, upstream)
        await server.peers[0].wait_gone(3)
    peer = server.peers[0]
    lag = (peer.reset_at - marks["sent"]) if peer.reset_at is not None and "sent" in marks else None
    return err.value, peer, lag


async def _open_beyond_size(peer, start):
    await peer.frame(FrameType.OPEN, 1, start.source_bytes)


async def _too_many_opens(peer, start):
    for i in range(1, MAX_OPENS + 2):
        await peer.frame(FrameType.OPEN, i, 0)
        await peer.frame(FrameType.CLOSE, i)


async def _three_open_streams(peer, start):
    for i in (1, 2, 3):
        await peer.frame(FrameType.OPEN, i, 0)


async def _reused_id(peer, start):
    await peer.frame(FrameType.OPEN, 1, 0)
    await peer.frame(FrameType.CLOSE, 1)
    await peer.frame(FrameType.OPEN, 1, 0)


async def _close_never_opened(peer, start):
    await peer.frame(FrameType.CLOSE, 9)


async def _window_not_requested(peer, start):
    await peer.frame(FrameType.WINDOW, 7, payload=b"opus")


async def _window_twice(peer, start):
    await peer.frame(FrameType.WINDOW, 0, payload=b"opus")
    await peer.frame(FrameType.WINDOW, 0, payload=b"opus")


async def _window_too_large(peer, start):
    await peer.header_only(int(FrameType.WINDOW), 0, 0, sc.CEILING_FILE_BYTES + 1)


async def _two_results(peer, start):
    await peer.frame(FrameType.WINDOW, 0, payload=b"opus")
    await peer.frame(FrameType.RESULT, payload=windows_result(start, [0]))
    await peer.frame(FrameType.RESULT, payload=windows_result(start, [0]), end=True)


async def _frame_after_result(peer, start):
    await peer.frame(FrameType.WINDOW, 0, payload=b"opus")
    await peer.frame(FrameType.RESULT, payload=windows_result(start, [0]))
    await peer.frame(FrameType.BEAT, end=True)


async def _malformed_result(peer, start):
    await peer.frame(FrameType.RESULT, payload=b'{"status":"ok","detail":"","media":{"status":"ok"}}', end=True)


async def _result_with_extra_key(peer, start):
    await peer.frame(FrameType.RESULT, payload=b'{"status":"error","detail":"","url":"x"}', end=True)


async def _result_window_without_frame(peer, start):
    await peer.frame(FrameType.RESULT, payload=windows_result(start, [0]), end=True)


async def _result_window_outside_request(peer, start):
    payload = windows_result(start, [0]).replace(b'"index":0', b'"index":5')
    await peer.frame(FrameType.WINDOW, 0, payload=b"opus")
    await peer.frame(FrameType.RESULT, payload=payload, end=True)


async def _body_frame_on_the_response(peer, start):
    await peer.frame(FrameType.DATA, 1, 0, b"x")


async def _unknown_frame_type(peer, start):
    await peer.header_only(99, 0, 0, 0)


async def _partial_frame_then_end(peer, start):
    await peer.send(b"\x06\x00", end=True)


HOSTILE = [
    _open_beyond_size, _too_many_opens, _three_open_streams, _reused_id, _close_never_opened, _window_not_requested,
    _window_twice, _window_too_large, _two_results, _frame_after_result, _malformed_result, _result_with_extra_key,
    _result_window_without_frame, _result_window_outside_request, _body_frame_on_the_response, _unknown_frame_type,
    _partial_frame_then_end,
]


class TestHostileServer:
    @pytest.mark.parametrize("tail", HOSTILE, ids=[f.__name__.lstrip("_") for f in HOSTILE])
    async def test_a_breach_resets_the_stream_and_fails_closed_within_a_second(self, tail):
        error, peer, lag = await _violating(tail, windows_start((0, 1)))
        assert isinstance(error, TunnelViolation)
        assert peer.client_reset == CANCEL
        assert lag is not None and lag <= 1.0
        assert not peer.client_ended.is_set()              # never a clean END_STREAM after a breach

    async def test_bytes_over_the_budget_are_a_breach(self):
        # Δ13 amended (14e701f7): the allowance is the budget plus OPEN_SLACK_BYTES per OPEN, so one read of a source
        # larger than budget + 1 x slack is the breach (one OPEN keeps the transfer to ~8 MiB, the fast gate's size).
        budget = 100 * 1024
        allowed = budget + OPEN_SLACK_BYTES
        size = allowed + 2 * DATA_MAX
        start = probe_start(size=size, budget=budget)

        async def read_past_the_allowance(peer, start):
            await peer.frame(FrameType.OPEN, 1, 0)          # one full read crosses budget + 1 x slack
            got = 0                                         # the lag is timed from just before the breach
            while got < allowed - DATA_MAX:
                frame = await peer.next_frame()
                if frame.kind is FrameType.DATA:
                    got += len(frame.payload)

        source = (SOURCE * (size // len(SOURCE) + 1))[:size]
        error, peer, lag = await _violating(read_past_the_allowance, start, MemoryRange(source, chunk=DATA_MAX))
        assert "budget" in str(error) and peer.client_reset == CANCEL and lag <= 1.0
        sent = sum(len(f.payload) for f in peer.body_frames if f.kind is FrameType.DATA)
        assert budget < sent <= allowed

    async def test_a_200_without_the_tunnel_outcome_is_a_breach(self):
        async def script(peer: Peer) -> None:
            await peer.next_frame()
            await peer.respond(200, sc.encode_outcome(sc.SandboxOutcome(SandboxStatus.OK, "")))
            await peer.wait_gone(3)

        async with FakeTunnelServer(script) as server:
            with pytest.raises(TunnelViolation):
                await run(server.url, probe_start())
            await server.peers[0].wait_gone(3)
        assert server.peers[0].client_reset == CANCEL


class TestDeadlines:
    async def test_silence_is_a_loss_at_the_idle_deadline(self):
        async def silent(peer: Peer) -> None:
            await peer.next_frame()
            await peer.respond()
            await peer.wait_gone(5)

        loop = asyncio.get_running_loop()
        async with FakeTunnelServer(silent) as server:
            began = loop.time()
            with pytest.raises(TunnelLost) as err:
                await run(server.url, probe_start(), idle_s=0.3)
            took = loop.time() - began
            await server.peers[0].wait_gone(3)
        assert err.value.kind == "lost" and "idle" in err.value.detail
        assert 0.25 <= took <= 1.0
        assert server.peers[0].client_reset == CANCEL

    def test_the_production_deadlines_are_the_designs(self):
        params = inspect.signature(SandboxTunnelClient).parameters
        assert params["idle_s"].default == IDLE_S == 30.0
        assert params["send_stall_s"].default == SEND_STALL_S == 30.0
        assert params["connect_s"].default == 10.0

    async def test_beats_keep_a_long_run_alive(self):
        async def beating(peer: Peer) -> None:
            start = await peer.start(SandboxOp.MEDIA_PROBE)
            await peer.respond()
            for _ in range(8):
                await asyncio.sleep(0.1)
                await peer.frame(FrameType.BEAT)
            await peer.frame(FrameType.RESULT, payload=probe_result(start), end=True)

        async with FakeTunnelServer(beating) as server:
            reply = await run(server.url, probe_start(), idle_s=0.3)
        assert reply.result.status is SandboxStatus.OK

    async def test_the_call_never_outlives_its_deadline(self):
        async def beating_forever(peer: Peer) -> None:
            await peer.next_frame()
            await peer.respond()
            while not peer.gone.is_set():
                await asyncio.sleep(0.05)
                await peer.frame(FrameType.BEAT)

        loop = asyncio.get_running_loop()
        async with FakeTunnelServer(beating_forever) as server:
            began = loop.time()
            with pytest.raises(TunnelLost) as err:
                await run(server.url, probe_start(), wall=0.4)
            took = loop.time() - began
            await server.peers[0].wait_gone(3)
        assert err.value.detail == "deadline" and took <= 0.4 + 0.2
        assert server.peers[0].client_reset == CANCEL

    async def test_a_flow_window_stuck_at_zero_is_a_loss(self):
        async def never_acks(peer: Peer) -> None:
            await peer.next_frame()
            await peer.respond()
            await peer.frame(FrameType.OPEN, 1, 0)
            while not peer.gone.is_set():
                await asyncio.sleep(0.05)
                await peer.frame(FrameType.BEAT)

        async with FakeTunnelServer(never_acks, ack=False) as server:
            with pytest.raises(TunnelLost) as err:
                await run(server.url, probe_start(), send_stall_s=0.3)
        assert "stalled" in err.value.detail


class TestLossesAndRefusals:
    @pytest.mark.parametrize("ending", ["reset", "goaway", "end_without_result", "close"])
    async def test_a_stream_that_goes_away_is_lost(self, ending):
        async def script(peer: Peer) -> None:
            await peer.next_frame()
            await peer.respond()
            await peer.frame(FrameType.BEAT)
            if ending == "reset":
                peer.reset()
            elif ending == "goaway":
                peer.goaway()
            elif ending == "end_without_result":
                await peer.end()
            else:
                peer.writer.transport.abort()
            await peer.wait_gone(3)

        async with FakeTunnelServer(script) as server:
            with pytest.raises(TunnelLost) as err:
                await run(server.url, probe_start())
        assert err.value.kind == "lost"

    @pytest.mark.parametrize("status, header, expected", [
        (503, sc.SandboxStatus.RECYCLING, "recycling"),
        (503, None, "no_answer"),
        (500, None, "lost"),
        (408, None, "lost"),
    ])
    async def test_answers_before_the_stream_that_may_be_retried(self, status, header, expected):
        async def script(peer: Peer) -> None:
            await peer.respond(status, sc.encode_outcome(sc.SandboxOutcome(header)) if header else None, end=True)

        async with FakeTunnelServer(script) as server:
            with pytest.raises(TunnelLost) as err:
                await run(server.url, probe_start())
        assert err.value.kind == expected

    async def test_the_id_token_failure_is_misconfigured(self):
        async def broken() -> str:
            raise RuntimeError("no metadata")

        loop = asyncio.get_running_loop()
        with pytest.raises(SandboxMisconfigured):
            await SandboxTunnelClient("http://127.0.0.1:9", broken).run(
                SandboxOp.MEDIA_PROBE, probe_start(), MemoryRange(), loop.time() + 1)

    def test_a_url_that_is_not_http_is_refused(self):
        with pytest.raises(ValueError):
            SandboxTunnelClient("ftp://light.example.com", id_token)

    async def test_start_of_another_op_is_refused(self):
        loop = asyncio.get_running_loop()
        with pytest.raises(ValueError):
            await client("http://127.0.0.1:9").run(SandboxOp.MEDIA_AUDIO_WINDOWS, probe_start(), MemoryRange(),
                                                   loop.time() + 1)

    async def test_a_tunnel_outcome_header_is_the_one_the_server_sends(self):
        assert sc.decode_outcome(TUNNEL_OUTCOME) == sc.SandboxOutcome(SandboxStatus.OK, "tunnel")
