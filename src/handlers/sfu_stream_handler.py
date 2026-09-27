"""
Cloudflare Realtime SFU WebSocket adapters — relay-side transport into VoiceSessionService,
the second transport next to Twilio's MediaStreamHandler (VOICE_WEB_TRANSPORT_RFC §5.4).

The SFU dials us twice per call, both URLs carrying the call ticket: `/sfu/egress` delivers the
caller's microphone, `/sfu/ingest` takes what we publish as Lelik's track. Wire format (POC
scripts/voice/cloudflare_sfu_poc/poc.py): protobuf Packet frames, PCM s16le 48 kHz stereo, 20 ms.
The provider session speaks PCM 24 kHz mono, so this handler resamples both ways.

REQ-ARCH-10/-25: domain + services only; the relay's max-instances=1 is what guarantees both
sockets of a ticket reach this process.
"""
import asyncio
import base64
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from websockets.exceptions import ConnectionClosed

from src.domain.paced_audio_outlet import PacedAudioOutlet
from src.domain.pcm_downsampler import PcmDownsampler
from src.domain.pcm_upsampler import PcmUpsampler
from src.domain.sfu_packet import decode_sfu_packet, encode_sfu_packet
from src.domain.voice_audio_format import PCM16_24K, PCM16_48K_STEREO
from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_playback_tracker import PlaybackTracker
from src.services.voice_session_service import VoiceSessionService
from src.utils.logger import logger

_FRAME_MS = 20
_FRAME_BYTES = PCM16_48K_STEREO.bytes_per_ms * _FRAME_MS  # 3840
_FRAME_SAMPLES = PCM16_48K_STEREO.sample_rate_hz * _FRAME_MS // 1000  # 960, the timestamp step
# The SFU garbage-collects a track that gets no packets for 30 s, so Lelik's silence is sent too
# (PacedAudioOutlet yields a silence frame whenever nothing is queued).
# `/sfu/*` is public: any socket naming an unknown ticket opens a registry entry that lives for
# the pair timeout. Bounding the not-yet-paired entries keeps junk connections from piling up.
_MAX_UNPAIRED_CALLS = 32


def parse_sfu_path(path: str) -> Optional[Tuple[str, str]]:
    """`/sfu/ingest?ticket=T` -> ("ingest", "T"); anything else -> None."""
    parts = urlsplit(path)
    kind = parts.path.rstrip("/").rsplit("/", 1)[-1]
    if not parts.path.startswith("/sfu/") or kind not in ("ingest", "egress"):
        return None
    ticket = (parse_qs(parts.query).get("ticket") or [""])[0]
    return (kind, ticket) if ticket else None


def is_ticket_shaped(ticket: str) -> bool:
    """Tickets are minted as `str(uuid.uuid4())`; anything else never came from us."""
    try:
        return str(uuid.UUID(ticket)) == ticket
    except ValueError:
        return False


@dataclass
class _SfuCall:
    ticket: str
    playback: PlaybackTracker = field(default_factory=lambda: PlaybackTracker(bytes_per_ms=PCM16_24K.bytes_per_ms))
    inbound: "asyncio.Queue[Optional[AudioFrame]]" = field(default_factory=asyncio.Queue)
    outlet: PacedAudioOutlet = field(default_factory=lambda: PacedAudioOutlet(_FRAME_BYTES, convert=PcmUpsampler()))
    downsampler: PcmDownsampler = field(default_factory=PcmDownsampler)
    ingest_ws: object = None
    egress_ws: object = None
    paired: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None

    def end(self) -> None:
        if not self.ended.is_set():
            self.ended.set()
            self.inbound.put_nowait(None)


class SfuStreamHandler:
    """Both SFU sockets of one call, relay-side. The first socket of a ticket starts the call;
    it runs once the other half arrives (VOICE_WEB_TRANSPORT_RFC §5.4)."""

    def __init__(self, session_service: VoiceSessionService, pair_timeout_s: float = 15.0,
                 egress_reattach_s: float = 5.0, frame_interval_s: float = _FRAME_MS / 1000,
                 max_unpaired_calls: int = _MAX_UNPAIRED_CALLS) -> None:
        self._session_service = session_service
        self._pair_timeout_s = pair_timeout_s
        self._egress_reattach_s = egress_reattach_s
        self._frame_interval_s = frame_interval_s
        self._max_unpaired_calls = max_unpaired_calls
        self._calls: Dict[str, _SfuCall] = {}

    async def handle_connection(self, ws, path: str) -> None:
        parsed = parse_sfu_path(path)
        if parsed is None:
            logger.warning(f"sfu stream: rejected connection to {path.split('?')[0]}")
            await ws.close()
            return
        kind, ticket = parsed
        if not is_ticket_shaped(ticket):
            logger.warning(f"sfu stream: rejected {kind} connection with a malformed ticket")
            await ws.close()
            return
        call = self._calls.get(ticket)
        if call is None:
            unpaired = sum(1 for c in self._calls.values() if not c.paired.is_set())
            if unpaired >= self._max_unpaired_calls:
                logger.warning(f"sfu stream: rejected {kind} connection, {unpaired} calls already "
                               f"waiting for their second socket")
                await ws.close()
                return
            call = _SfuCall(ticket=ticket)
            self._calls[ticket] = call
            call.task = asyncio.ensure_future(self._run(call))
        if kind == "ingest":
            await self._serve_ingest(call, ws)
        else:
            await self._serve_egress(call, ws)

    async def _run(self, call: _SfuCall) -> None:
        try:
            try:
                await asyncio.wait_for(call.paired.wait(), timeout=self._pair_timeout_s)
            except asyncio.TimeoutError:
                logger.warning(f"sfu stream {call.ticket}: second socket never arrived, dropping")
                return
            await self._session_service.handle_call(
                ticket=call.ticket,
                inbound_audio=self._inbound_frames(call),
                send_outbound_audio=lambda frame: self._enqueue(call, frame),
                clear_outbound_audio=lambda: self._clear(call),
                playback=call.playback,
            )
        except Exception:
            logger.error(f"sfu stream {call.ticket}: call failed", exc_info=True)
        finally:
            call.end()
            for ws in (call.ingest_ws, call.egress_ws):
                if ws is not None:
                    await ws.close()
            self._calls.pop(call.ticket, None)
            logger.info(f"sfu stream {call.ticket}: call closed")

    @staticmethod
    async def _inbound_frames(call: _SfuCall) -> AsyncIterator[AudioFrame]:
        while True:
            frame = await call.inbound.get()
            if frame is None:
                return
            yield frame

    def _mark_paired(self, call: _SfuCall) -> None:
        if call.ingest_ws is not None and call.egress_ws is not None:
            call.paired.set()

    async def _serve_egress(self, call: _SfuCall, ws) -> None:
        call.egress_ws = ws
        self._mark_paired(call)
        logger.info(f"sfu stream {call.ticket}: egress connected")
        try:
            async for message in ws:
                if call.ended.is_set():
                    break
                if not isinstance(message, (bytes, bytearray)):
                    logger.warning(f"sfu stream {call.ticket}: non-binary egress message ignored")
                    continue
                try:
                    packet = decode_sfu_packet(message)
                except ValueError as exc:
                    logger.warning(f"sfu stream {call.ticket}: malformed egress packet skipped: {exc}")
                    continue
                pcm24 = call.downsampler(packet.payload)
                if pcm24:
                    call.inbound.put_nowait(AudioFrame(
                        encoding=PCM16_24K.encoding, sample_rate_hz=PCM16_24K.sample_rate_hz,
                        payload=base64.b64encode(pcm24).decode("ascii"), track="inbound",
                    ))
        except ConnectionClosed as exc:
            # Routine: the SFU drops egress on hangup and between stream-mode retries.
            logger.info(f"sfu stream {call.ticket}: egress closed abnormally ({exc})")
        except Exception:
            logger.error(f"sfu stream {call.ticket}: egress read failed", exc_info=True)
        if call.egress_ws is not ws or call.ended.is_set():
            return
        # Stream mode: the SFU retries the same endpoint for up to 5 s; a new egress re-attaches.
        await asyncio.sleep(self._egress_reattach_s)
        if call.egress_ws is ws and not call.ended.is_set():
            logger.info(f"sfu stream {call.ticket}: egress gone and not reattached, ending call")
            call.end()
            if not call.paired.is_set():
                self._calls.pop(call.ticket, None)

    async def _serve_ingest(self, call: _SfuCall, ws) -> None:
        if call.ingest_ws is not None:
            logger.warning(f"sfu stream {call.ticket}: duplicate ingest socket closed")
            await ws.close()
            return
        call.ingest_ws = ws
        self._mark_paired(call)
        logger.info(f"sfu stream {call.ticket}: ingest connected")
        drain = asyncio.ensure_future(self._drain(call, ws))
        try:
            await self._pace(call, ws, drain)
        finally:
            drain.cancel()
            call.end()
            # Mirrors MediaStreamHandler.handle_connection's finally: don't return to the
            # caller (and let it think this socket is done) before `_run`'s own finally has
            # actually closed it. Without this, `_pace`'s pair-timeout return races `_run`'s
            # identical timeout independently, and `handle_connection` can return with the
            # ingest socket still open.
            if call.task is not None:
                await call.task

    async def _drain(self, call: _SfuCall, ws) -> None:
        # The ingest socket carries nothing to us; iterating it is how its close is noticed.
        # Everything is caught here: `_pace` only polls `drain.done()`, so an exception left
        # in this task would surface as "Task exception was never retrieved".
        try:
            async for _ in ws:
                pass
        except ConnectionClosed as exc:
            logger.info(f"sfu stream {call.ticket}: ingest closed abnormally ({exc})")
            return
        except Exception:
            logger.error(f"sfu stream {call.ticket}: ingest read failed", exc_info=True)
            return
        logger.info(f"sfu stream {call.ticket}: ingest closed by the SFU")

    async def _pace(self, call: _SfuCall, ws, drain: asyncio.Task) -> None:
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(call.paired.wait(), timeout=self._pair_timeout_s)
        except asyncio.TimeoutError:
            return
        sequence = 0
        next_at = loop.time()
        while not call.ended.is_set() and not drain.done():
            frame, played_marks = call.outlet.next_frame()
            try:
                await ws.send(encode_sfu_packet(sequence, sequence * _FRAME_SAMPLES, frame))
            except Exception:
                logger.info(f"sfu stream {call.ticket}: ingest send failed, ending call")
                return
            sequence += 1
            for mark in played_marks:
                call.playback.record_played(mark)
            next_at += self._frame_interval_s
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_at = loop.time()
                await asyncio.sleep(0)

    @staticmethod
    async def _enqueue(call: _SfuCall, frame: AudioFrame) -> None:
        mark = call.playback.record_sent(frame.payload)
        call.outlet.push(base64.b64decode(frame.payload), mark)

    @staticmethod
    async def _clear(call: _SfuCall) -> None:
        # Same end state as Twilio's echoed marks after `clear`: nothing queued, all sent "played".
        call.outlet.clear()
        call.playback.record_played(str(call.playback.sent_bytes))
