import json
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import websockets

from src.domain.voice_audio_frame import AudioFrame
from src.ports.realtime_session_port import RealtimeSessionEvent, RealtimeSessionPort
from src.utils.logger import logger

_MODEL = "gpt-realtime-2.1"
_CACHE_BOUNDARY = "<!-- CACHE_BOUNDARY -->"


def _strip_cache_boundary(instructions: str) -> str:
    return instructions.replace(_CACHE_BOUNDARY, "")


class OpenAIRealtimeAdapter(RealtimeSessionPort):
    """RealtimeSessionPort against OpenAI's Realtime API (GA session shape,
    verified live during Phase 0 spikes - see RFC §4.7 and §9)."""

    def __init__(self, api_key: str, model: str = _MODEL, ws_connect: Callable = websockets.connect) -> None:
        self._api_key = api_key
        self._model = model
        self._connect = ws_connect
        self._ws = None

    async def open(self, instructions: str, reasoning_effort: str, tools: List[dict]) -> None:
        url = f"wss://api.openai.com/v1/realtime?model={self._model}"
        self._ws = await self._connect(url, additional_headers={"Authorization": f"Bearer {self._api_key}"})
        session: Dict[str, Any] = {
            "type": "realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcmu"}},
                "output": {"format": {"type": "audio/pcmu"}},
            },
            "reasoning": {"effort": reasoning_effort},
            "instructions": _strip_cache_boundary(instructions),
        }
        if tools:
            session["tools"] = tools
            session["tool_choice"] = "auto"
        await self._ws.send(json.dumps({"type": "session.update", "session": session}))

    async def send_audio(self, frame: AudioFrame) -> None:
        # payload arrives already base64-encoded mulaw off the wire (RFC §4.4/§4.7) - AudioFrame.payload
        # holds the ascii bytes of that base64 string, so this only decodes to str, never re-encodes.
        payload = frame.payload if isinstance(frame.payload, str) else frame.payload.decode("ascii")
        await self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": payload}))

    async def receive_events(self) -> AsyncIterator[RealtimeSessionEvent]:
        async for raw in self._ws:
            event = json.loads(raw)
            normalized = self._normalize(event)
            if normalized is not None:
                yield normalized

    def _normalize(self, event: dict) -> Optional[RealtimeSessionEvent]:
        event_type = event.get("type")
        if event_type in ("response.output_audio.delta", "response.audio.delta"):
            payload = event.get("delta") or event.get("audio")
            frame = AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload=payload, track="outbound")
            return RealtimeSessionEvent(type="audio_delta", payload={"frame": frame})
        if event_type == "response.function_call_arguments.done":
            return RealtimeSessionEvent(
                type="tool_call",
                payload={"call_id": event.get("call_id"), "name": event.get("name"), "arguments": event.get("arguments")},
            )
        if event_type == "response.done":
            usage = (event.get("response") or {}).get("usage", {})
            return RealtimeSessionEvent(type="response_done", payload={"usage": usage})
        if event_type == "response.created":
            return RealtimeSessionEvent(type="response_created", payload={})
        if event_type == "conversation.item.input_audio_transcription.completed":
            return RealtimeSessionEvent(type="user_transcript", payload={"text": event.get("transcript", "")})
        if event_type == "response.output_audio_transcript.done":
            return RealtimeSessionEvent(type="model_transcript", payload={"text": event.get("transcript", "")})
        if event_type == "input_audio_buffer.speech_started":
            return RealtimeSessionEvent(type="speech_started", payload={})
        if event_type == "input_audio_buffer.speech_stopped":
            return RealtimeSessionEvent(type="speech_stopped", payload={})
        if event_type == "error":
            logger.error(f"OpenAI realtime error event: {event.get('error', event)}")
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
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
