"""SfuStreamHandler: Cloudflare Realtime SFU WebSocket adapters as a relay transport (VOICE_WEB_TRANSPORT_RFC §5.4)."""
import asyncio
import base64

import numpy as np
import pytest

from src.domain.sfu_packet import decode_sfu_packet, encode_sfu_packet
from src.domain.voice_audio_frame import AudioFrame
from src.handlers.sfu_stream_handler import SfuStreamHandler, parse_sfu_path

FRAME = 3840
# Tickets are uuid4 strings; the handler rejects anything else (final-review finding 7).
T1 = "5f0c1a3e-8d2b-4c1f-9a7e-2b6d4e8f1a03"


class FakeSfuWs:
    """Server-side WebSocket as the handler sees it: iterate incoming, send, close."""

    def __init__(self):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[bytes] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def send(self, data):
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(data)

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)


class FakeSessionService:
    def __init__(self, behaviour=None):
        self.calls = []
        self._behaviour = behaviour

    async def handle_call(self, ticket, inbound_audio, send_outbound_audio, clear_outbound_audio, playback):
        record = {"ticket": ticket, "frames": [], "playback": playback,
                  "send": send_outbound_audio, "clear": clear_outbound_audio}
        self.calls.append(record)
        if self._behaviour:
            await self._behaviour(record)
        async for frame in inbound_audio:
            record["frames"].append(frame)


def _mic_packet(seq: int) -> bytes:
    tone = (np.sin(np.arange(960) / 3) * 8000).astype("<i2")
    return encode_sfu_packet(seq, seq * 960, np.repeat(tone, 2).astype("<i2").tobytes())


def _handler(service, **kw):
    kw.setdefault("frame_interval_s", 0.001)
    kw.setdefault("egress_reattach_s", 0.05)  # tests end calls by closing egress; don't wait 5 s
    return SfuStreamHandler(service, **kw)


def test_parse_sfu_path():
    assert parse_sfu_path("/sfu/ingest?ticket=t1") == ("ingest", "t1")
    assert parse_sfu_path("/sfu/egress?ticket=t2&x=1") == ("egress", "t2")
    assert parse_sfu_path("/sfu/other?ticket=t") is None
    assert parse_sfu_path("/sfu/ingest") is None
    assert parse_sfu_path("/") is None


@pytest.mark.asyncio
async def test_both_halves_start_one_call_and_mic_reaches_it_as_pcm24k():
    service = FakeSessionService()
    handler = _handler(service)
    ingest, egress = FakeSfuWs(), FakeSfuWs()
    tasks = [asyncio.ensure_future(handler.handle_connection(ingest, f"/sfu/ingest?ticket={T1}")),
             asyncio.ensure_future(handler.handle_connection(egress, f"/sfu/egress?ticket={T1}"))]
    await egress.incoming.put(_mic_packet(1))
    await egress.incoming.put(_mic_packet(2))
    await asyncio.sleep(0.05)
    await egress.close()  # egress gone, nobody reattaches -> call ends after the reattach window
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)

    assert len(service.calls) == 1 and service.calls[0]["ticket"] == T1
    frames = service.calls[0]["frames"]
    assert frames and all(f.encoding == "audio/pcm" and f.sample_rate_hz == 24000 for f in frames)
    # Each 20 ms SFU frame (960 stereo samples) becomes 480 mono samples = 960 bytes at 24 kHz.
    assert sum(len(base64.b64decode(f.payload)) for f in frames) == 2 * 960
    assert ingest.closed


@pytest.mark.asyncio
async def test_pair_timeout_closes_a_lonely_half_without_starting_a_call():
    service = FakeSessionService()
    handler = _handler(service, pair_timeout_s=0.05)
    ingest = FakeSfuWs()
    await asyncio.wait_for(handler.handle_connection(ingest, f"/sfu/ingest?ticket={T1}"), timeout=1)
    assert service.calls == [] and ingest.closed


@pytest.mark.asyncio
async def test_egress_reconnect_within_window_keeps_the_same_call():
    service = FakeSessionService()
    handler = _handler(service, egress_reattach_s=0.3)
    ingest, egress1, egress2 = FakeSfuWs(), FakeSfuWs(), FakeSfuWs()
    t_in = asyncio.ensure_future(handler.handle_connection(ingest, f"/sfu/ingest?ticket={T1}"))
    t_e1 = asyncio.ensure_future(handler.handle_connection(egress1, f"/sfu/egress?ticket={T1}"))
    await asyncio.sleep(0.02)
    await egress1.close()
    await asyncio.sleep(0.05)
    t_e2 = asyncio.ensure_future(handler.handle_connection(egress2, f"/sfu/egress?ticket={T1}"))
    await egress2.incoming.put(_mic_packet(3))
    await asyncio.sleep(0.4)  # longer than the reattach window: the call must still be alive
    assert len(service.calls) == 1 and not ingest.closed
    await egress2.close()
    await asyncio.wait_for(asyncio.gather(t_in, t_e1, t_e2), timeout=2)
    assert service.calls[0]["frames"]


@pytest.mark.asyncio
async def test_ingest_close_ends_the_call():
    service = FakeSessionService()
    handler = _handler(service)
    ingest, egress = FakeSfuWs(), FakeSfuWs()
    tasks = [asyncio.ensure_future(handler.handle_connection(ingest, f"/sfu/ingest?ticket={T1}")),
             asyncio.ensure_future(handler.handle_connection(egress, f"/sfu/egress?ticket={T1}"))]
    await asyncio.sleep(0.02)
    await ingest.close()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    assert egress.closed


@pytest.mark.asyncio
async def test_pacer_sends_silence_then_speech_and_flushes_a_partial_tail():
    async def speak(record):
        pcm24 = b"\x10\x00" * 1500  # 1500 samples = 62.5 ms: not a whole number of 20 ms frames
        await record["send"](AudioFrame("audio/pcm", 24000, base64.b64encode(pcm24).decode(), "outbound"))

    service = FakeSessionService(behaviour=speak)
    handler = _handler(service)
    ingest, egress = FakeSfuWs(), FakeSfuWs()
    tasks = [asyncio.ensure_future(handler.handle_connection(ingest, f"/sfu/ingest?ticket={T1}")),
             asyncio.ensure_future(handler.handle_connection(egress, f"/sfu/egress?ticket={T1}"))]
    await asyncio.sleep(0.15)
    playback = service.calls[0]["playback"]
    assert playback.sent_bytes == 3000 and playback.caught_up
    await egress.close()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)

    packets = [decode_sfu_packet(p) for p in ingest.sent]
    assert all(len(p.payload) == FRAME for p in packets)
    assert [p.sequence_number for p in packets] == list(range(len(packets)))
    assert all(b.timestamp - a.timestamp == 960 for a, b in zip(packets, packets[1:]))
    assert sum(1 for p in packets if any(p.payload)) == 4  # 62.5 ms of speech -> 4 frames, tail padded


@pytest.mark.asyncio
async def test_clear_drops_queued_speech_and_marks_it_played():
    async def speak_then_clear(record):
        pcm24 = b"\x10\x00" * 24000  # one second
        await record["send"](AudioFrame("audio/pcm", 24000, base64.b64encode(pcm24).decode(), "outbound"))
        await record["clear"]()

    service = FakeSessionService(behaviour=speak_then_clear)
    handler = _handler(service, frame_interval_s=0.02)
    ingest, egress = FakeSfuWs(), FakeSfuWs()
    tasks = [asyncio.ensure_future(handler.handle_connection(ingest, f"/sfu/ingest?ticket={T1}")),
             asyncio.ensure_future(handler.handle_connection(egress, f"/sfu/egress?ticket={T1}"))]
    await asyncio.sleep(0.1)
    assert service.calls[0]["playback"].caught_up
    await egress.close()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    assert sum(1 for p in ingest.sent if any(decode_sfu_packet(p).payload)) <= 2


@pytest.mark.asyncio
async def test_malformed_and_text_messages_are_skipped():
    service = FakeSessionService()
    handler = _handler(service)
    ingest, egress = FakeSfuWs(), FakeSfuWs()
    tasks = [asyncio.ensure_future(handler.handle_connection(ingest, f"/sfu/ingest?ticket={T1}")),
             asyncio.ensure_future(handler.handle_connection(egress, f"/sfu/egress?ticket={T1}"))]
    await egress.incoming.put("hello")
    await egress.incoming.put(b"\x2a\x05ab")
    await egress.incoming.put(_mic_packet(1))
    await asyncio.sleep(0.05)
    await egress.close()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    assert service.calls[0]["frames"]
