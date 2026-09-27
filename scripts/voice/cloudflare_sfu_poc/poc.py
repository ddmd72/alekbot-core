#!/usr/bin/env python3
"""
POC: browser WebRTC -> Cloudflare Realtime SFU -> WebSocket adapters -> this process.

Answers, before any production code is written (memory: project_voice_web_call_cloudflare):
  A. MODE=echo   - does the WebSocket adapter work end to end, what frame size/cadence does the
                   SFU deliver, is silence sent continuously, how much latency the loop adds.
  B. MODE=openai - the same loop with OpenAI Realtime (PCM 24 kHz) in the middle: time from the
                   caller's end of speech to Lelik's first audio, compared with the Twilio relay's
                   `first audio after ... ms` log lines.
  C. Any mode    - bytes each way, printed as GB/hour and $ at the verified rates, to compare with
                   the Cloudflare dashboard after a 10-15 minute call.

Topology (same shape as Twilio Media Streams today - the SFU dials US):
  browser --WebRTC--> SFU --WS egress adapter (mic, PCM 48k stereo)--> /ws/egress/<call>
  browser <--WebRTC-- SFU <--WS ingest adapter (PCM 48k stereo)------- /ws/ingest/<call>

Run:
  1. .env: CLOUDFLARE_SFU_APP_ID, CLOUDFLARE_SFU_APP_SECRET (and OPENAI_API_KEY for MODE=openai).
  2. cloudflared tunnel --url http://localhost:8787      -> note the https://<x>.trycloudflare.com host
  3. MODE=echo venv/bin/python scripts/voice/cloudflare_sfu_poc/poc.py --public-host <x>.trycloudflare.com
  4. Open https://<x>.trycloudflare.com on a laptop (headphones!) or an iPhone, press Call.

The App Secret never reaches the browser: every SFU API call is made from here.
"""
import argparse
import asyncio
import base64
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
import numpy as np
from aiohttp import web
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")

# Everything printed also goes to a gitignored file, so the results can be read back without
# copying the terminal.
LOG_PATH = ROOT / "scripts" / "memory" / "cloudflare_sfu_poc.log"


class _Tee:
    def __init__(self, stream, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = stream
        self._file = open(path, "a", buffering=1, encoding="utf-8")

    def write(self, text: str) -> int:
        self._file.write(text)
        return self._stream.write(text)

    def flush(self) -> None:
        self._file.flush()
        self._stream.flush()


sys.stdout = _Tee(sys.stdout, LOG_PATH)

API_BASE = "https://rtc.live.cloudflare.com/v1/apps/{app_id}"
MODE = os.getenv("MODE", "echo")  # echo | openai
if MODE not in ("echo", "openai"):
    sys.exit(f"MODE must be 'echo' or 'openai', got {MODE!r}")
# Ping-tone onset detector on the mic stream (the browser mutes the mic around a ping, so the
# first loud frame after quiet is the tone). Wall clock: on a laptop test the browser shares it.
TONE_RMS = 3000
TONE_QUIET_FRAMES = 10
OPENAI_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
OPENAI_VOICE = "verse"

# SFU adapter PCM: s16le, 48 kHz, stereo interleaved (developers.cloudflare.com websocket-adapter).
SFU_RATE = 48000
SFU_CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAME_MS = 20
SFU_FRAME_BYTES = SFU_RATE * FRAME_MS // 1000 * SFU_CHANNELS * BYTES_PER_SAMPLE  # 3840
SFU_BYTES_PER_S = SFU_RATE * SFU_CHANNELS * BYTES_PER_SAMPLE  # 192000
SILENCE_FRAME = bytes(SFU_FRAME_BYTES)
# "continuous": a silence frame every 20 ms while Lelik is quiet (worst case for GCP egress).
# "heartbeat": one silence frame per second - enough for the SFU's 30 s inactivity GC?
SILENCE_POLICY = os.getenv("SILENCE", "continuous")
ECHO_MAX_QUEUED_FRAMES = 3  # 60 ms of jitter absorption in echo mode

# Verified rates (2026-09-27): Cloudflare Realtime egress $0.05/GB after 1000 GB/mo free;
# GCP Premium internet egress from Americas $0.12/GiB after 1 GiB/mo free.
CF_USD_PER_GB = 0.05
GCP_USD_PER_GIB = 0.12


# --------------------------------------------------------------------------------------------
# Minimal protobuf for `message Packet { uint32 sequenceNumber = 1; uint32 timestamp = 2;
# bytes payload = 5; }` - three fields do not justify a protoc build step.
# --------------------------------------------------------------------------------------------

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(buf: bytes, pos: int):
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def encode_packet(seq: int, ts: int, payload: bytes) -> bytes:
    return (b"\x08" + _varint(seq & 0xFFFFFFFF) + b"\x10" + _varint(ts & 0xFFFFFFFF)
            + b"\x2a" + _varint(len(payload)) + payload)


def decode_packet(buf: bytes):
    seq = ts = None
    payload = b""
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field_no, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            if field_no == 1:
                seq = value
            elif field_no == 2:
                ts = value
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            if field_no == 5:
                payload = buf[pos:pos + length]
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return seq, ts, payload


# --------------------------------------------------------------------------------------------
# Resampling 48k stereo <-> 24k mono (what the production transport adapter will have to do).
# --------------------------------------------------------------------------------------------

# 31-tap windowed-sinc low-pass at 11 kHz for the 48k -> 24k decimation.
_TAPS = np.sinc(np.arange(-15, 16) * (11000 * 2 / SFU_RATE)) * np.hamming(31)
_TAPS = (_TAPS / _TAPS.sum()).astype(np.float32)


class Downsampler:
    """48k stereo s16le -> 24k mono s16le, filter state carried across chunks."""

    def __init__(self) -> None:
        self._tail = np.zeros(len(_TAPS) - 1, dtype=np.float32)
        self._phase = 0

    def __call__(self, pcm: bytes) -> bytes:
        stereo = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        mono = stereo.reshape(-1, 2).mean(axis=1)
        x = np.concatenate([self._tail, mono])
        filtered = np.convolve(x, _TAPS, mode="valid")
        self._tail = x[-(len(_TAPS) - 1):]
        out = filtered[self._phase::2]
        self._phase = (self._phase + len(filtered)) % 2
        return np.clip(out, -32768, 32767).astype("<i2").tobytes()


class Upsampler:
    """24k mono s16le -> 48k stereo s16le by linear interpolation, last sample carried."""

    def __init__(self) -> None:
        self._last = 0.0

    def __call__(self, pcm: bytes) -> bytes:
        mono = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        if not len(mono):
            return b""
        prev = np.concatenate([[self._last], mono[:-1]])
        self._last = float(mono[-1])
        doubled = np.empty(len(mono) * 2, dtype=np.float32)
        doubled[0::2] = (prev + mono) / 2
        doubled[1::2] = mono
        stereo = np.repeat(doubled, 2)
        return np.clip(stereo, -32768, 32767).astype("<i2").tobytes()


# --------------------------------------------------------------------------------------------
# Per-call state and measurements
# --------------------------------------------------------------------------------------------

@dataclass
class Stats:
    started: float = field(default_factory=time.monotonic)
    egress_msgs: int = 0
    egress_ws_bytes: int = 0  # serialized protobuf bytes CF -> us (billed by CF)
    egress_payload_bytes: int = 0
    egress_silent_msgs: int = 0
    egress_payload_sizes: Dict[int, int] = field(default_factory=dict)
    egress_last_at: Optional[float] = None
    egress_gaps_ms: List[float] = field(default_factory=list)
    egress_seq_gaps: int = 0
    egress_last_seq: Optional[int] = None
    egress_ts_deltas: Dict[int, int] = field(default_factory=dict)
    egress_last_ts: Optional[int] = None
    ingest_msgs: int = 0
    ingest_ws_bytes: int = 0  # serialized protobuf bytes us -> CF (billed by GCP in production)
    ingest_speech_msgs: int = 0
    echo_dropped_frames: int = 0
    quiet_frames: int = 0
    first_audio_ms: List[int] = field(default_factory=list)  # end of caller speech -> first delta
    first_audio_from_response_ms: List[int] = field(default_factory=list)

    def report(self, label: str) -> str:
        elapsed = max(time.monotonic() - self.started, 1e-6)
        hours = elapsed / 3600
        gaps = sorted(self.egress_gaps_ms)

        def pct(p: float) -> str:
            return f"{gaps[min(int(len(gaps) * p), len(gaps) - 1)]:.1f}" if gaps else "-"

        cf_gb = self.egress_ws_bytes / 1e9
        gcp_gib = self.ingest_ws_bytes / 2**30
        lines = [
            f"==== {label}: {elapsed:.0f} s, mode={MODE}, silence={SILENCE_POLICY} ====",
            f"egress (CF->us): {self.egress_msgs} msgs, {self.egress_ws_bytes} B on wire, "
            f"payload sizes {dict(sorted(self.egress_payload_sizes.items()))}, "
            f"silent msgs {self.egress_silent_msgs}, seq gaps {self.egress_seq_gaps}",
            f"  inter-arrival ms p50={pct(0.5)} p95={pct(0.95)} p99={pct(0.99)} max={gaps[-1] if gaps else '-'}",
            f"  timestamp deltas {dict(sorted(self.egress_ts_deltas.items())[:6])}",
            f"ingest (us->CF): {self.ingest_msgs} msgs ({self.ingest_speech_msgs} speech), "
            f"{self.ingest_ws_bytes} B on wire, echo frames dropped by the 60 ms cap: {self.echo_dropped_frames}",
            f"per hour: CF->us {cf_gb / hours:.3f} GB/h, us->CF {gcp_gib / hours:.3f} GiB/h",
            f"$ per hour (no free tiers): CF ${cf_gb / hours * CF_USD_PER_GB:.4f}, "
            f"GCP ${gcp_gib / hours * GCP_USD_PER_GIB:.4f}",
        ]
        if self.first_audio_ms:
            lines.append(f"first audio after end of speech (ms): {self.first_audio_ms}")
            lines.append(f"first audio after response.created (ms): {self.first_audio_from_response_ms}")
        return "\n".join(lines)


@dataclass
class Call:
    call_id: str
    browser_session_id: str
    outbound: "asyncio.Queue[bytes]" = field(default_factory=asyncio.Queue)  # 48k stereo PCM chunks
    pending: bytearray = field(default_factory=bytearray)
    stats: Stats = field(default_factory=Stats)
    adapter_ids: List[str] = field(default_factory=list)
    ingest_session_id: Optional[str] = None
    openai_ws: Optional[aiohttp.ClientWebSocketResponse] = None
    downsampler: Downsampler = field(default_factory=Downsampler)
    upsampler: Upsampler = field(default_factory=Upsampler)
    speech_stopped_at: Optional[float] = None
    response_created_at: Optional[float] = None
    awaiting_first_audio: bool = False
    tasks: List[asyncio.Task] = field(default_factory=list)

    def clear_outbound(self) -> None:
        self.pending.clear()
        while not self.outbound.empty():
            self.outbound.get_nowait()


CALLS: Dict[str, Call] = {}


# --------------------------------------------------------------------------------------------
# Cloudflare Realtime SFU HTTPS API
# --------------------------------------------------------------------------------------------

class Sfu:
    def __init__(self, app_id: str, secret: str, http: aiohttp.ClientSession) -> None:
        self._base = API_BASE.format(app_id=app_id)
        self._headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
        self._http = http

    async def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        async with self._http.request(method, self._base + path, headers=self._headers,
                                      data=json.dumps(body) if body is not None else None) as resp:
            text = await resp.text()
            print(f"[sfu] {method} {path} -> {resp.status} {text[:600]}")
            if resp.status >= 400:
                raise web.HTTPBadGateway(text=f"SFU {path}: {resp.status} {text}")
            return json.loads(text) if text else {}

    async def new_session(self) -> str:
        return (await self._call("POST", "/sessions/new"))["sessionId"]

    async def tracks_new(self, session_id: str, body: dict) -> dict:
        return await self._call("POST", f"/sessions/{session_id}/tracks/new", body)

    async def renegotiate(self, session_id: str, sdp: str) -> dict:
        return await self._call("PUT", f"/sessions/{session_id}/renegotiate",
                                {"sessionDescription": {"type": "answer", "sdp": sdp}})

    async def adapter_new(self, track: dict) -> dict:
        return await self._call("POST", "/adapters/websocket/new", {"tracks": [track]})

    async def adapter_close(self, adapter_id: str) -> dict:
        return await self._call("POST", "/adapters/websocket/close", {"tracks": [{"adapterId": adapter_id}]})


# --------------------------------------------------------------------------------------------
# HTTP routes (browser signalling)
# --------------------------------------------------------------------------------------------

async def index(_request: web.Request) -> web.Response:
    return web.FileResponse(Path(__file__).with_name("page.html"))


async def publish(request: web.Request) -> web.Response:
    """Browser offer with its mic transceiver -> SFU answer."""
    body = await request.json()
    sfu: Sfu = request.app["sfu"]
    session_id = await sfu.new_session()
    resp = await sfu.tracks_new(session_id, {
        "sessionDescription": {"type": "offer", "sdp": body["sdp"]},
        "tracks": [{"location": "local", "mid": body["mid"], "trackName": "mic"}],
    })
    call = Call(call_id=uuid.uuid4().hex[:12], browser_session_id=session_id)
    CALLS[call.call_id] = call
    return web.json_response({"callId": call.call_id, "sdp": resp["sessionDescription"]["sdp"]})


async def connect(request: web.Request) -> web.Response:
    """Once the browser's PeerConnection is up: wire both adapters, then have the browser
    pull the ingest-published track (returns an SFU offer to answer)."""
    body = await request.json()
    call = CALLS[body["callId"]]
    sfu: Sfu = request.app["sfu"]
    host = request.app["public_host"]
    if MODE == "openai":
        await open_openai(call, request.app["http"])
    ingest = await sfu.adapter_new({
        "location": "local", "trackName": "lelik", "inputCodec": "pcm",
        "endpoint": f"wss://{host}/ws/ingest/{call.call_id}",
    })
    track = ingest["tracks"][0]
    call.adapter_ids.append(track["adapterId"])
    call.ingest_session_id = track["sessionId"]
    egress = await sfu.adapter_new({
        "location": "remote", "sessionId": call.browser_session_id, "trackName": "mic",
        "outputCodec": "pcm", "endpoint": f"wss://{host}/ws/egress/{call.call_id}",
    })
    call.adapter_ids.append(egress["tracks"][0]["adapterId"])
    pull = await sfu.tracks_new(call.browser_session_id, {
        "tracks": [{"location": "remote", "sessionId": call.ingest_session_id, "trackName": "lelik"}],
    })
    return web.json_response({
        "requiresImmediateRenegotiation": pull.get("requiresImmediateRenegotiation"),
        "sdp": (pull.get("sessionDescription") or {}).get("sdp"),
    })


async def renegotiate(request: web.Request) -> web.Response:
    body = await request.json()
    call = CALLS[body["callId"]]
    await request.app["sfu"].renegotiate(call.browser_session_id, body["sdp"])
    return web.json_response({"ok": True})


async def hangup(request: web.Request) -> web.Response:
    body = await request.json()
    call = CALLS.pop(body["callId"], None)
    if call is None:
        return web.json_response({"ok": False})
    for adapter_id in call.adapter_ids:
        try:
            await request.app["sfu"].adapter_close(adapter_id)
        except web.HTTPException:
            pass
    if call.openai_ws is not None:
        await call.openai_ws.close()
    for task in call.tasks:
        task.cancel()
    report = call.stats.report(f"call {call.call_id} ended")
    print(report)
    return web.json_response({"report": report})


async def client_log(request: web.Request) -> web.Response:
    """Browser-side measurements (echo round trip, WebRTC stats) printed next to server ones."""
    print(f"[browser] {(await request.text())[:1000]}")
    return web.json_response({"ok": True})


# --------------------------------------------------------------------------------------------
# WebSocket endpoints the SFU dials
# --------------------------------------------------------------------------------------------

async def ws_egress(request: web.Request) -> web.WebSocketResponse:
    """User's mic, decoded by the SFU to 48k stereo PCM."""
    call = CALLS.get(request.match_info["call_id"])
    ws = web.WebSocketResponse(max_msg_size=64 * 1024)
    await ws.prepare(request)
    print(f"[egress] SFU connected for {request.match_info['call_id']} headers={dict(request.headers)}")
    if call is None:
        await ws.close()
        return ws
    stats = call.stats
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.BINARY:
            print(f"[egress] non-binary message: {msg.type} {str(msg.data)[:200]}")
            continue
        now = time.monotonic()
        seq, ts, payload = decode_packet(msg.data)
        stats.egress_msgs += 1
        stats.egress_ws_bytes += len(msg.data)
        stats.egress_payload_bytes += len(payload)
        stats.egress_payload_sizes[len(payload)] = stats.egress_payload_sizes.get(len(payload), 0) + 1
        if not any(payload):
            stats.egress_silent_msgs += 1
        if stats.egress_last_at is not None:
            stats.egress_gaps_ms.append((now - stats.egress_last_at) * 1000)
        stats.egress_last_at = now
        if seq is not None and stats.egress_last_seq is not None and seq != stats.egress_last_seq + 1:
            stats.egress_seq_gaps += 1
        stats.egress_last_seq = seq
        if ts is not None and stats.egress_last_ts is not None:
            delta = ts - stats.egress_last_ts
            stats.egress_ts_deltas[delta] = stats.egress_ts_deltas.get(delta, 0) + 1
        stats.egress_last_ts = ts
        if stats.egress_msgs == 1:
            print(f"[egress] first packet seq={seq} ts={ts} payload={len(payload)} B")
        rms = float(np.sqrt(np.mean(np.frombuffer(payload, dtype="<i2").astype(np.float32) ** 2))) if payload else 0.0
        if rms >= TONE_RMS and stats.quiet_frames >= TONE_QUIET_FRAMES:
            print(f"[egress] onset at wall={time.time() * 1000:.0f} rms={rms:.0f}")
        stats.quiet_frames = 0 if rms >= TONE_RMS else stats.quiet_frames + 1
        if MODE == "echo":
            # A live source paced at exactly its arrival rate never drains a backlog that a jitter
            # burst built up (first echo run: 27 frames = 540 ms stuck for the whole call). Cap it;
            # OpenAI audio (faster than real time) is left unbounded - there the queue IS playback.
            while call.outbound.qsize() >= ECHO_MAX_QUEUED_FRAMES:
                call.outbound.get_nowait()
                stats.echo_dropped_frames += 1
            await call.outbound.put(payload)
        elif MODE == "openai" and call.openai_ws is not None and not call.openai_ws.closed:
            pcm24 = call.downsampler(payload)
            await call.openai_ws.send_str(json.dumps({
                "type": "input_audio_buffer.append", "audio": base64.b64encode(pcm24).decode("ascii"),
            }))
    print(f"[egress] closed for {call.call_id}")
    return ws


async def ws_ingest(request: web.Request) -> web.WebSocketResponse:
    """What the SFU publishes as track `lelik`: paced 20 ms frames from `call.outbound`."""
    call = CALLS.get(request.match_info["call_id"])
    ws = web.WebSocketResponse(max_msg_size=64 * 1024)
    await ws.prepare(request)
    print(f"[ingest] SFU connected for {request.match_info['call_id']}")
    if call is None:
        await ws.close()
        return ws

    async def drain() -> None:
        async for msg in ws:
            print(f"[ingest] SFU sent {msg.type} {str(msg.data)[:200]}")

    reader = asyncio.ensure_future(drain())
    call.tasks.append(reader)
    seq = 0
    samples = 0
    next_at = time.monotonic()
    last_silence = 0.0
    try:
        while not ws.closed:
            # Top up one frame from queued audio (echo payloads or upsampled OpenAI audio).
            while len(call.pending) < SFU_FRAME_BYTES and not call.outbound.empty():
                call.pending.extend(call.outbound.get_nowait())
            now = time.monotonic()
            if len(call.pending) >= SFU_FRAME_BYTES:
                frame = bytes(call.pending[:SFU_FRAME_BYTES])
                del call.pending[:SFU_FRAME_BYTES]
                call.stats.ingest_speech_msgs += 1
            elif SILENCE_POLICY == "continuous" or now - last_silence >= 1.0:
                frame = SILENCE_FRAME
                last_silence = now
            else:
                frame = None
            if frame is not None:
                packet = encode_packet(seq, samples, frame)
                await ws.send_bytes(packet)
                seq += 1
                samples += SFU_FRAME_BYTES // (SFU_CHANNELS * BYTES_PER_SAMPLE)
                call.stats.ingest_msgs += 1
                call.stats.ingest_ws_bytes += len(packet)
            next_at += FRAME_MS / 1000
            delay = next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_at = time.monotonic()
    finally:
        reader.cancel()
        print(f"[ingest] closed for {call.call_id}")
    return ws


# --------------------------------------------------------------------------------------------
# OpenAI Realtime leg (MODE=openai)
# --------------------------------------------------------------------------------------------

async def open_openai(call: Call, http: aiohttp.ClientSession) -> None:
    ws = await http.ws_connect(
        f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        max_msg_size=0,
    )
    call.openai_ws = ws
    await ws.send_str(json.dumps({"type": "session.update", "session": {
        "type": "realtime",
        "output_modalities": ["audio"],
        "instructions": ("Ты Лелик, живой и остроумный собеседник. Говори по-русски, коротко, "
                         "естественно, как в телефонном разговоре с другом."),
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                # POC only: the provider replies and interrupts on its own; the relay does this itself.
                "turn_detection": {"type": "semantic_vad", "eagerness": "low",
                                   "create_response": True, "interrupt_response": True},
            },
            "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": OPENAI_VOICE},
        },
    }}))
    call.tasks.append(asyncio.ensure_future(openai_events(call, ws)))


async def openai_events(call: Call, ws: aiohttp.ClientWebSocketResponse) -> None:
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        event = json.loads(msg.data)
        etype = event.get("type")
        now = time.monotonic()
        if etype in ("response.output_audio.delta", "response.audio.delta"):
            pcm24 = base64.b64decode(event.get("delta") or event.get("audio") or "")
            if call.awaiting_first_audio:
                call.awaiting_first_audio = False
                if call.speech_stopped_at is not None:
                    call.stats.first_audio_ms.append(round((now - call.speech_stopped_at) * 1000))
                if call.response_created_at is not None:
                    call.stats.first_audio_from_response_ms.append(round((now - call.response_created_at) * 1000))
                print(f"[openai] first audio: {call.stats.first_audio_ms[-1:]} ms after speech end")
            await call.outbound.put(call.upsampler(pcm24))
        elif etype == "input_audio_buffer.speech_started":
            # Barge-in: our paced queue IS the playback buffer, so dropping it silences Lelik at once.
            call.clear_outbound()
            print("[openai] speech_started -> outbound cleared")
        elif etype == "input_audio_buffer.speech_stopped":
            call.speech_stopped_at = now
            call.awaiting_first_audio = True
        elif etype == "response.created":
            call.response_created_at = now
        elif etype == "session.updated":
            print(f"[openai] session.updated audio={event['session'].get('audio')}")
        elif etype == "error":
            print(f"[openai] ERROR {event}")


# --------------------------------------------------------------------------------------------

async def periodic_report() -> None:
    while True:
        await asyncio.sleep(30)
        for call in list(CALLS.values()):
            print(call.stats.report(f"call {call.call_id} running"))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-host", required=True, help="cloudflared host, e.g. abc.trycloudflare.com")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    app_id = os.environ.get("CLOUDFLARE_SFU_APP_ID")
    secret = os.environ.get("CLOUDFLARE_SFU_APP_SECRET")
    if not app_id or not secret:
        sys.exit("CLOUDFLARE_SFU_APP_ID / CLOUDFLARE_SFU_APP_SECRET missing in .env")
    if MODE == "openai" and not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY missing in .env")

    http = aiohttp.ClientSession()
    app = web.Application()
    app["http"] = http
    app["sfu"] = Sfu(app_id, secret, http)
    app["public_host"] = args.public_host
    app.add_routes([
        web.get("/", index),
        web.post("/api/publish", publish),
        web.post("/api/connect", connect),
        web.post("/api/renegotiate", renegotiate),
        web.post("/api/hangup", hangup),
        web.post("/api/log", client_log),
        web.get("/ws/egress/{call_id}", ws_egress),
        web.get("/ws/ingest/{call_id}", ws_ingest),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", args.port).start()
    print(f"\n#### {time.strftime('%Y-%m-%d %H:%M:%S')} POC mode={MODE} silence={SILENCE_POLICY} "
          f"on :{args.port} -> https://{args.public_host}/ (log: {LOG_PATH})")
    reporter = asyncio.ensure_future(periodic_report())
    try:
        await asyncio.Event().wait()
    finally:
        reporter.cancel()
        await http.close()


if __name__ == "__main__":
    asyncio.run(main())
