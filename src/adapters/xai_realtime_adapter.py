import json
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import websockets

from src.domain.voice_audio_format import MULAW_8K, AudioFormat
from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_turn_ownership import TurnOwnership
from src.ports.realtime_session_port import RealtimeSessionEvent, RealtimeSessionPort
from src.utils.logger import logger

_MODEL = "grok-voice-think-fast-2.0"
_CACHE_BOUNDARY = "<!-- CACHE_BOUNDARY -->"
# Male, multilingual (GET /v1/tts/voices, 2026-09-29). The call's VoiceSessionSpec picks the voice.
_VOICE = "rex"
# xAI has no semantic_vad; server_vad ends a turn on silence. 700 ms held in the probe
# (decisions/voice_xai_protocol_probe.md); a UAT knob if mid-thought pauses cut the caller off.
_SILENCE_DURATION_MS = 800
# xAI replies and stops on interruption by itself, and ignores create_response=false anyway
# (probe, 2026-09-29), so the adapter implements PROVIDER turn ownership only.
_TURN_DETECTION = {
    "type": "server_vad",
    "silence_duration_ms": _SILENCE_DURATION_MS,
    "create_response": True,
    "interrupt_response": True,
}


def _strip_cache_boundary(instructions: str) -> str:
    return instructions.replace(_CACHE_BOUNDARY, "")


def _wire_format(audio_format: AudioFormat) -> Dict[str, Any]:
    """μ-law is fixed at 8 kHz on xAI; PCM names its rate."""
    if audio_format.encoding == "audio/pcmu":
        return {"type": "audio/pcmu"}
    return {"type": audio_format.encoding, "rate": audio_format.sample_rate_hz}


def _flatten_usage(usage: dict) -> Dict[str, float]:
    """xAI's response.done usage → flat legs for VoiceCallBuffer.add_usage.

    xAI bills per minute of audio and reports the billable part itself
    (`billable_audio_seconds`); that and text input are the priced legs
    (domain.billing). Token legs ride along for observability only.
    """
    input_details = usage.get("input_token_details") or {}
    output_details = usage.get("output_token_details") or {}
    return {
        "audio_input_tokens": input_details.get("audio_tokens", 0),
        "audio_output_tokens": output_details.get("audio_tokens", 0),
        "text_input_tokens": input_details.get("text_tokens", 0),
        "text_output_tokens": output_details.get("text_tokens", 0),
        "billable_audio_seconds": usage.get("billable_audio_seconds", 0),
    }


class XaiRealtimeAdapter(RealtimeSessionPort):
    """RealtimeSessionPort against xAI's Voice Agent API (OpenAI-GA-compatible events;
    probed live 2026-09-29, decisions/voice_xai_protocol_probe.md). Provider-owned turns:
    xAI starts every reply to the caller and stops it when talked over
    (VOICE_MULTI_PROVIDER_RFC §4.3/§4.4)."""

    supported_turn_ownership = frozenset({TurnOwnership.PROVIDER})

    def __init__(self, api_key: str, model: str = _MODEL, voice: str = _VOICE,
                 ws_connect: Callable = websockets.connect, audio_format: AudioFormat = MULAW_8K) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._connect = ws_connect
        self._audio_format = audio_format
        self._ws = None

    async def open(self, instructions: str, reasoning_effort: str, tools: List[dict]) -> None:
        url = f"wss://api.x.ai/v1/realtime?model={self._model}"
        self._ws = await self._connect(url, additional_headers={"Authorization": f"Bearer {self._api_key}"})
        session: Dict[str, Any] = {
            "voice": self._voice,
            "instructions": _strip_cache_boundary(instructions),
            "turn_detection": _TURN_DETECTION,
            "audio": {
                "input": {"format": _wire_format(self._audio_format)},
                "output": {"format": _wire_format(self._audio_format)},
            },
            "reasoning": {"effort": reasoning_effort},
        }
        if tools:
            session["tools"] = [{"type": "function", **tool} for tool in tools]
            session["tool_choice"] = "auto"
        await self._ws.send(json.dumps({"type": "session.update", "session": session}))

    async def send_audio(self, frame: AudioFrame) -> None:
        payload = frame.payload if isinstance(frame.payload, str) else frame.payload.decode("ascii")
        await self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": payload}))

    async def receive_events(self) -> AsyncIterator[RealtimeSessionEvent]:
        async for raw in self._ws:
            normalized = self._normalize(json.loads(raw))
            if normalized is not None:
                yield normalized

    def _normalize(self, event: dict) -> Optional[RealtimeSessionEvent]:
        event_type = event.get("type")
        if event_type in ("response.output_audio.delta", "response.audio.delta"):
            frame = AudioFrame(encoding=self._audio_format.encoding,
                               sample_rate_hz=self._audio_format.sample_rate_hz,
                               payload=event.get("delta"), track="outbound")
            return RealtimeSessionEvent(
                type="audio_delta", payload={"frame": frame, "item_id": event.get("item_id")}
            )
        if event_type == "response.function_call_arguments.done":
            return RealtimeSessionEvent(
                type="tool_call",
                payload={"call_id": event.get("call_id"), "name": event.get("name"), "arguments": event.get("arguments")},
            )
        if event_type == "response.created":
            return RealtimeSessionEvent(type="response_created", payload={})
        if event_type == "response.done":
            # Usage sits at the event's top level on xAI, not under `response`.
            usage = _flatten_usage(event.get("usage") or {})
            return RealtimeSessionEvent(type="response_done", payload={"usage": usage, "model": self._model})
        if event_type == "conversation.item.input_audio_transcription.completed":
            return RealtimeSessionEvent(type="user_transcript", payload={"text": event.get("transcript", "")})
        if event_type == "response.output_audio_transcript.done":
            return RealtimeSessionEvent(type="model_transcript", payload={"text": event.get("transcript", "")})
        if event_type == "input_audio_buffer.committed":
            return RealtimeSessionEvent(type="turn_committed", payload={"item_id": event.get("item_id")})
        if event_type == "input_audio_buffer.speech_started":
            return RealtimeSessionEvent(type="speech_started", payload={})
        if event_type == "input_audio_buffer.speech_stopped":
            return RealtimeSessionEvent(type="speech_stopped", payload={})
        if event_type == "error":
            logger.error(f"xAI realtime error event: {event.get('error', event)}")
            return RealtimeSessionEvent(type="error", payload={"message": str(event.get("error", event))})
        return None

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": output},
        }))

    async def submit_message(self, role: str, text: str) -> None:
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]},
        }))

    async def request_response(self) -> None:
        # The greeting and delegation answers: the only replies the relay starts under PROVIDER ownership.
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def cancel_response(self) -> None:
        # Not called under PROVIDER ownership (xAI stops its own reply); kept for the port contract.
        await self._ws.send(json.dumps({"type": "response.cancel"}))

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        await self._ws.send(json.dumps({
            "type": "conversation.item.truncate",
            "item_id": item_id,
            "content_index": 0,
            "audio_end_ms": audio_end_ms,
        }))

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
