"""The caller closes first (2026-11-04): on RESULT the client ends its request half at once and sends nothing more, so the
sandbox can end the response after it. A response ended while the request half was open made Cloud Run's front end abort
a 26 MB answer with RST_STREAM INTERNAL_ERROR (`sandbox_tunnel_app._await_body_end`)."""
from __future__ import annotations

import asyncio

from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_tunnel import FrameType
from tests.unit.adapters.sandbox_client.test_sandbox_tunnel_client import (
    MemoryRange,
    probe_start,
    run,
    windows_start,
)
from tests.unit.adapters.sandbox_client.tunnel_h2_fake import FakeTunnelServer, Peer, probe_result, windows_result


async def test_the_client_ends_its_body_on_result_before_the_response_ends():
    seen = {}

    async def script(peer: Peer) -> None:
        start = await peer.start(SandboxOp.MEDIA_AUDIO_WINDOWS)
        await peer.respond()
        await peer.frame(FrameType.WINDOW, 0, payload=b"opus-0" * 1000)
        await peer.frame(FrameType.WINDOW, 1, payload=b"opus-1")
        await peer.frame(FrameType.RESULT, payload=windows_result(start, [0, 1]))     # no END_STREAM yet
        await asyncio.wait_for(peer.client_ended.wait(), timeout=5)
        seen["client_ended_first"] = True
        await peer.end()

    async with FakeTunnelServer(script) as server:
        reply = await run(server.url, windows_start())
    assert server.errors == []
    assert seen == {"client_ended_first": True}
    assert reply.result.status is SandboxStatus.OK
    assert reply.windows == ((0, b"opus-0" * 1000), (1, b"opus-1"))
    assert server.peers[0].client_reset is None


async def test_an_upstream_still_feeding_at_result_stops_and_the_call_succeeds():
    """RESULT arrives while the client still streams a range: no DATA after its END_STREAM, no false `lost`."""
    seen = {}

    async def script(peer: Peer) -> None:
        start = await peer.start(SandboxOp.MEDIA_PROBE)
        await peer.respond()
        await peer.frame(FrameType.OPEN, 1, 0)
        while not any(f.kind is FrameType.DATA for f in peer.body_frames):
            await asyncio.sleep(0.01)
        await peer.frame(FrameType.RESULT, payload=probe_result(start))
        await asyncio.wait_for(peer.client_ended.wait(), timeout=5)
        seen["frames_at_end"] = len(peer.body_frames)
        await asyncio.sleep(0.2)                       # anything the client still sent would land here
        seen["frames_later"] = len(peer.body_frames)
        await peer.end()

    async with FakeTunnelServer(script) as server:
        reply = await run(server.url, probe_start(), MemoryRange(chunk=4096, delay=0.01))
    assert server.errors == []
    assert reply.result.status is SandboxStatus.OK and reply.result.media.duration_s == 1834.5
    assert seen["frames_later"] == seen["frames_at_end"]
    peer = server.peers[0]
    assert peer.client_reset is None and peer.body_violation is None
