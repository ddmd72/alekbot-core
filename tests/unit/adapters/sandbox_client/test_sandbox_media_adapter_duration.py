"""SandboxMediaAdapter.duration_s (VideoDurationPort) over the scripted loopback sandbox (S5b)."""
import asyncio
import logging

import pytest

from src.adapters.sandbox_client.sandbox_media_adapter import SandboxMediaAdapter
from src.adapters.sandbox_client.sandbox_tunnel_client import SandboxTunnelClient
from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_tunnel import FrameType, TunnelProbe, TunnelResult
from tests.unit.adapters.sandbox_client.test_sandbox_media_adapter import id_token, refusing, resetting
from tests.unit.adapters.sandbox_client.tunnel_h2_fake import (
    FakeTunnelServer,
    Peer,
    encode_result,
    failure_result,
    outcome,
    probe_result,
)

VIDEO = bytes((i * 11 + 1) % 247 for i in range(300_000))


def sandbox(url: str) -> SandboxMediaAdapter:
    return SandboxMediaAdapter(SandboxTunnelClient(url, id_token))


def probing_with(result_for, seen=None):
    async def script(peer: Peer) -> None:
        start = await peer.start(SandboxOp.MEDIA_PROBE)
        if seen is not None:
            seen["start"] = start
        await peer.respond()
        got = await peer.serve_stream(1, 0, start.source_bytes)
        if seen is not None:
            seen["bytes"] = got
        await peer.frame(FrameType.RESULT, payload=result_for(start), end=True)
    return script


async def test_a_known_duration_is_returned():
    async with FakeTunnelServer(probing_with(lambda s: probe_result(s, duration_s=7.5))) as server:
        assert await sandbox(server.url).duration_s(VIDEO) == 7.5


async def test_an_unknown_duration_is_none():
    def result(start):
        return encode_result(TunnelResult(status=SandboxStatus.OK, media=TunnelProbe(
            duration_s=0.0, duration_known=False, has_audio=False, container="mov,mp4")), start)

    async with FakeTunnelServer(probing_with(result)) as server:
        assert await sandbox(server.url).duration_s(VIDEO) is None


@pytest.mark.parametrize("status", [SandboxStatus.TIMEOUT, SandboxStatus.MEMORY, SandboxStatus.ERROR,
                                    SandboxStatus.TOO_LARGE, SandboxStatus.BAD_REQUEST])
async def test_a_run_that_did_not_succeed_is_none(status):
    async with FakeTunnelServer(probing_with(lambda s: failure_result(s, status, "x"))) as server:
        assert await sandbox(server.url).duration_s(VIDEO) is None


@pytest.mark.parametrize("scripts", [(refusing(503, outcome(SandboxStatus.RECYCLING)),), (refusing(401),),
                                     (resetting(),)], ids=["busy", "misconfigured", "lost-twice"])
async def test_an_unavailable_or_lost_sandbox_is_none_and_logged(scripts, caplog):
    with caplog.at_level(logging.WARNING):
        async with FakeTunnelServer(*scripts) as server:
            assert await sandbox(server.url).duration_s(VIDEO) is None
    assert any("video duration unknown" in r.getMessage() for r in caplog.records)


async def test_empty_bytes_are_none_without_a_request():
    async with FakeTunnelServer(probing_with(probe_result)) as server:
        assert await sandbox(server.url).duration_s(b"") is None
    assert server.peers == []


async def test_the_sandbox_receives_the_video_from_memory_in_a_probe_start():
    seen = {}
    async with FakeTunnelServer(probing_with(probe_result, seen)) as server:
        await sandbox(server.url).duration_s(VIDEO)
    assert seen["bytes"] == VIDEO
    assert seen["start"].op is SandboxOp.MEDIA_PROBE and seen["start"].source_bytes == len(VIDEO)


class _RaisingClient:
    """A tunnel client whose call raises `exc`: the adapter's own handling of the failure, nothing on the wire."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, *args, **kwargs):
        raise self._exc


async def test_an_unexpected_failure_is_none_and_logged(caplog):
    adapter = SandboxMediaAdapter(_RaisingClient(RuntimeError("boom")))
    with caplog.at_level(logging.WARNING):
        assert await adapter.duration_s(VIDEO) is None
    assert "video duration unknown: RuntimeError" in caplog.text


async def test_a_cancelled_probe_is_not_swallowed():
    adapter = SandboxMediaAdapter(_RaisingClient(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await adapter.duration_s(VIDEO)
