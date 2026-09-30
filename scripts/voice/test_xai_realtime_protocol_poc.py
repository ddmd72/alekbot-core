#!/usr/bin/env python3
"""
POC: does xAI's realtime API honour the switches VoiceSessionService depends on?

VoiceSessionService owns the turn cycle (persona anchor, then its own response.create) and
barge-in (response.cancel, then conversation.item.truncate). On OpenAI that rests on
turn_detection.create_response=False and interrupt_response=False. xAI does not document either,
and every provider `error` event ends a call, so each is checked live here before an adapter is
written. macOS only: test speech is synthesized with `say` + `afconvert` so server_vad has real
speech to detect.

Usage: python scripts/voice/test_xai_realtime_protocol_poc.py [case ...]
Cases: config, autoreply, bargein, idle_cancel, pcm24, latency (default: all).
"""
import asyncio
import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
from src.config.settings import load_settings

import websockets

MODEL = "grok-voice-think-fast-2.0"
URL = f"wss://api.x.ai/v1/realtime?model={MODEL}"
UTTERANCE_1 = "Hello, can you hear me? Please tell me a long story about an old lighthouse keeper."
UTTERANCE_2 = "Wait, stop, stop. Actually, what time is it?"
TOOL = {
    "type": "function",
    "name": "delegate_to_specialist",
    "description": "Forward a request the assistant cannot answer itself.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}
# The audio-event noise is summarized, not logged one line per chunk.
_QUIET = {"response.output_audio.delta", "response.audio.delta", "response.output_audio_transcript.delta", "ping"}


def _speech(text: str, fmt: str) -> bytes:
    """Raw audio bytes of `text`: fmt 'ulaw' = G.711 mu-law 8 kHz, 'pcm24' = LE16 24 kHz."""
    data_format = "ulaw@8000" if fmt == "ulaw" else "LEI16@24000"
    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = os.path.join(tmp, "s.aiff"), os.path.join(tmp, "s.wav")
        subprocess.run(["say", "-o", aiff, text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", data_format, "-c", "1", aiff, wav], check=True)
        blob = open(wav, "rb").read()
    pos = 12
    while pos < len(blob):
        chunk_id, size = blob[pos:pos + 4], struct.unpack("<I", blob[pos + 4:pos + 8])[0]
        if chunk_id == b"data":
            return blob[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    raise RuntimeError("no data chunk")


def _silence(seconds: float, fmt: str) -> bytes:
    return b"\xff" * int(8000 * seconds) if fmt == "ulaw" else b"\x00\x00" * int(24000 * seconds)


def _session(fmt: str = "ulaw", effort: str = "high", create_response: bool = False,
             interrupt_response: bool = False) -> dict:
    audio_format = {"type": "audio/pcmu"} if fmt == "ulaw" else {"type": "audio/pcm", "rate": 24000}
    return {
        "voice": "eve",
        "instructions": "You are a friendly phone companion. Keep replies conversational.",
        "turn_detection": {
            "type": "server_vad",
            "silence_duration_ms": 700,
            "create_response": create_response,
            "interrupt_response": interrupt_response,
        },
        "audio": {
            "input": {"format": audio_format},
            "output": {"format": audio_format},
        },
        "reasoning": {"effort": effort},
        "tools": [TOOL],
        "tool_choice": "auto",
    }


class Probe:
    def __init__(self, ws, name: str):
        self.ws, self.name, self.t0 = ws, name, time.monotonic()
        self.events: list = []
        self.audio_chunks = 0

    def _log(self, direction: str, event: dict) -> None:
        kind = event.get("type")
        self.events.append({"t": round(time.monotonic() - self.t0, 3), "dir": direction, **event})
        if direction == "recv" and kind in _QUIET:
            self.audio_chunks += kind != "ping"
            return
        brief = {k: v for k, v in event.items() if k not in ("type", "audio", "delta", "session")}
        text = json.dumps(brief, ensure_ascii=False)
        print(f"[{self.name} {time.monotonic() - self.t0:6.2f}s] {direction} {kind} {text[:400]}")

    async def send(self, event: dict) -> None:
        self._log("send", {k: v for k, v in event.items() if k != "audio"})
        await self.ws.send(json.dumps(event))

    async def stream(self, audio: bytes, fmt: str) -> None:
        """Push audio in real time, 20 ms frames, the way a Twilio/SFU leg would."""
        frame = 160 if fmt == "ulaw" else 960
        for i in range(0, len(audio), frame):
            await self.ws.send(json.dumps({
                "type": "input_audio_buffer.append", "audio": base64.b64encode(audio[i:i + frame]).decode(),
            }))
            await asyncio.sleep(0.02)

    async def recv_until(self, predicate, timeout: float):
        """Read events until predicate(event) is true or timeout; returns the matching event or None."""
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), left)
            except asyncio.TimeoutError:
                return None
            if isinstance(raw, bytes):
                continue
            event = json.loads(raw)
            self._log("recv", event)
            if predicate(event):
                return event
        return None

    def seen(self, kind: str, after: float = 0.0) -> list:
        return [e for e in self.events if e["dir"] == "recv" and e.get("type") == kind and e["t"] >= after]

    def now(self) -> float:
        return time.monotonic() - self.t0


async def _open(name: str, api_key: str, session: dict) -> Probe:
    ws = await websockets.connect(URL, additional_headers={"Authorization": f"Bearer {api_key}"}, max_size=None)
    probe = Probe(ws, name)
    await probe.send({"type": "session.update", "session": session})
    await probe.recv_until(lambda e: e.get("type") in ("session.updated", "error"), 10)
    return probe


async def case_config(key: str) -> dict:
    """Does session.update with our switches come back as session.updated, and what does it echo?"""
    probe = await _open("config", key, _session())
    updated = probe.seen("session.updated")
    errors = probe.seen("error")
    await probe.ws.close()
    echoed = updated[-1].get("session", {}) if updated else {}
    print(json.dumps(echoed, indent=2, ensure_ascii=False)[:3000])
    return {"session_updated": bool(updated), "errors": errors, "echoed_turn_detection": echoed.get("turn_detection"),
            "echoed_reasoning": echoed.get("reasoning"), "echoed_audio": echoed.get("audio")}


async def case_autoreply(key: str) -> dict:
    """With create_response=False, does a committed turn get a reply we did not ask for? Then:
    is a role=system item accepted, and does our own response.create produce audio + usage?"""
    probe = await _open("autoreply", key, _session())
    await probe.stream(_speech(UTTERANCE_1, "ulaw") + _silence(1.5, "ulaw"), "ulaw")
    committed = await probe.recv_until(lambda e: "committed" in (e.get("type") or ""), 10)
    t_commit = probe.now()
    await probe.recv_until(lambda e: False, 6)  # anything the server does on its own
    auto_created = probe.seen("response.created")
    await probe.send({"type": "conversation.item.create", "item": {
        "type": "message", "role": "system",
        "content": [{"type": "input_text", "text": "Reply in one short sentence."}]}})
    await probe.recv_until(lambda e: e.get("type") in ("conversation.item.added", "conversation.item.created", "error"), 5)
    t_ask = probe.now()
    await probe.send({"type": "response.create"})
    done = await probe.recv_until(lambda e: e.get("type") == "error" or (
        e.get("type") == "response.done" and e.get("response", {}).get("status") != "cancelled"), 40)
    await probe.recv_until(lambda e: False, 3)  # late transcripts
    await probe.ws.close()
    all_done = probe.seen("response.done")
    first_audio = probe.seen("response.output_audio.delta", after=t_ask) or probe.seen("response.audio.delta", after=t_ask)
    return {
        "committed_event": committed and committed.get("type"),
        "auto_response_created": len(auto_created),
        "system_item_errors": [e for e in probe.seen("error", after=t_commit)],
        "reply_done": done and done.get("type"),
        "response_done_events": [{"status": e.get("response", {}).get("status"), "usage": e.get("usage"),
                                  "top_keys": sorted(k for k in e if k not in ("response",))} for e in all_done],
        "model_transcripts": [e.get("transcript") for e in probe.seen("response.output_audio_transcript.done")],
        "first_audio_s": round(first_audio[0]["t"] - t_ask, 2) if first_audio else None,
        "audio_chunks": probe.audio_chunks,
        "recv_event_types": sorted({e.get("type") for e in probe.events if e["dir"] == "recv"}),
    }


async def case_bargein(key: str) -> dict:
    """While a long reply is generating, the caller speaks. With interrupt_response=False the
    provider must NOT cancel on its own; then our response.cancel and truncate must be accepted."""
    probe = await _open("bargein", key, _session())
    await probe.send({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "Tell me a long story, at least two minutes, about a lighthouse keeper."}]}})
    await probe.send({"type": "response.create"})
    first = await probe.recv_until(lambda e: e.get("type") in ("response.output_audio.delta", "response.audio.delta"), 30)
    item_id = first and first.get("item_id")
    await probe.recv_until(lambda e: False, 2)
    t_speak = probe.now()
    speaker = asyncio.create_task(probe.stream(_speech(UTTERANCE_2, "ulaw") + _silence(1.5, "ulaw"), "ulaw"))
    await probe.recv_until(lambda e: e.get("type") == "response.done", 6)
    await speaker
    auto_done = probe.seen("response.done", after=t_speak)
    t_cancel = probe.now()
    if not auto_done:
        await probe.send({"type": "response.cancel"})
        await probe.recv_until(lambda e: e.get("type") in ("response.done", "error"), 10)
    await probe.send({"type": "conversation.item.truncate", "item_id": item_id, "content_index": 0, "audio_end_ms": 1500})
    await probe.recv_until(lambda e: e.get("type") in ("conversation.item.truncated", "error"), 5)
    await probe.recv_until(lambda e: False, 4)
    await probe.ws.close()
    return {
        "speech_events_after_speaking": sorted({e["type"] for e in probe.events
                                                 if e["dir"] == "recv" and e["t"] >= t_speak and "speech" in e["type"]}),
        "provider_auto_cancelled": [e.get("response", {}).get("status") for e in auto_done],
        "cancel_result": [e.get("response", {}).get("status") for e in probe.seen("response.done", after=t_cancel)],
        "truncated": bool(probe.seen("conversation.item.truncated")),
        "errors": probe.seen("error"),
        "auto_response_after_speech": len(probe.seen("response.created", after=t_speak)),
    }


async def case_vad_timing(key: str) -> dict:
    """When does speech_started arrive relative to the audio's actual speech onset? Barge-in
    needs it while the caller is still talking. Compared across interrupt_response and the
    default session (no turn_detection override)."""
    out = {}
    for label, session in (("interrupt_false", _session()), ("interrupt_true", _session(interrupt_response=True)),
                           ("provider_default", {k: v for k, v in _session().items() if k != "turn_detection"})):
        probe = await _open(f"vad-{label}", key, session)
        t_audio = probe.now()
        speaker = asyncio.create_task(probe.stream(_speech(UTTERANCE_1, "ulaw") + _silence(2.0, "ulaw"), "ulaw"))
        await probe.recv_until(lambda e: e.get("type") == "input_audio_buffer.committed", 20)
        await speaker
        await probe.recv_until(lambda e: False, 2)
        await probe.ws.close()
        started = probe.seen("input_audio_buffer.speech_started")
        stopped = probe.seen("input_audio_buffer.speech_stopped")
        out[label] = {
            "speech_started_wall_s": started and round(started[0]["t"] - t_audio, 2),
            "speech_started_audio_start_ms": started and started[0].get("audio_start_ms"),
            "speech_stopped_wall_s": stopped and round(stopped[0]["t"] - t_audio, 2),
            "speech_stopped_audio_end_ms": stopped and stopped[0].get("audio_end_ms"),
            "response_created_wall_s": [round(e["t"] - t_audio, 2) for e in probe.seen("response.created")],
            "echoed_turn_detection": (probe.seen("session.updated") or [{}])[-1].get("session", {}).get("turn_detection"),
        }
    return out


async def case_idle_cancel(key: str) -> dict:
    """response.cancel with no response active: error or no-op?"""
    probe = await _open("idle_cancel", key, _session())
    await probe.send({"type": "response.cancel"})
    await probe.recv_until(lambda e: e.get("type") == "error", 4)
    await probe.ws.close()
    return {"errors": probe.seen("error")}


async def case_pcm24(key: str) -> dict:
    """The web path's format: PCM16 24 kHz in and out, speech-triggered turn, our response.create."""
    probe = await _open("pcm24", key, _session(fmt="pcm24"))
    await probe.stream(_speech(UTTERANCE_1, "pcm24") + _silence(1.5, "pcm24"), "pcm24")
    committed = await probe.recv_until(lambda e: "committed" in (e.get("type") or ""), 10)
    await probe.send({"type": "response.create"})
    done = await probe.recv_until(lambda e: e.get("type") in ("response.done", "error"), 40)
    await probe.recv_until(lambda e: False, 3)
    await probe.ws.close()
    return {"committed": committed and committed.get("type"), "done": done and done.get("type"),
            "audio_chunks": probe.audio_chunks, "errors": probe.seen("error"),
            "user_transcripts": [e for e in probe.events if "input_audio_transcription" in (e.get("type") or "")][-2:]}


async def case_latency(key: str) -> dict:
    """Time from response.create to first audio, reasoning effort high vs none, 3 runs each."""
    out = {}
    for effort in ("none", "high"):
        times = []
        for _ in range(3):
            probe = await _open(f"latency-{effort}", key, _session(effort=effort))
            await probe.send({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "What's a good name for a cat? One sentence."}]}})
            t = probe.now()
            await probe.send({"type": "response.create"})
            first = await probe.recv_until(lambda e: e.get("type") in ("response.output_audio.delta", "response.audio.delta", "error"), 30)
            times.append(round(probe.now() - t, 2) if first and first.get("type") != "error" else None)
            await probe.recv_until(lambda e: e.get("type") == "response.done", 20)
            await probe.ws.close()
        out[effort] = times
    return out


CASES = {"config": case_config, "autoreply": case_autoreply, "bargein": case_bargein,
         "idle_cancel": case_idle_cancel, "vad_timing": case_vad_timing, "pcm24": case_pcm24, "latency": case_latency}


async def main() -> None:
    key = (load_settings().get("XAI_API_KEY") or "").strip()  # the stored secret ends in "\n"
    results = {}
    for name in sys.argv[1:] or list(CASES):
        print(f"\n===== {name} =====")
        try:
            results[name] = await CASES[name](key)
        except Exception as exc:
            results[name] = {"exception": repr(exc)}
        print(f"RESULT {name}: {json.dumps(results[name], ensure_ascii=False, default=str)[:2000]}")
    print("\n===== SUMMARY =====")
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
