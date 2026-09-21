import json
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import websockets

from src.domain.voice_audio_frame import AudioFrame
from src.ports.realtime_session_port import RealtimeSessionEvent, RealtimeSessionPort
from src.utils.logger import logger

_MODEL = "gpt-realtime-2.1"
_CACHE_BOUNDARY = "<!-- CACHE_BOUNDARY -->"
# Input-audio transcription model. Reuses the codebase's existing default transcription
# model (OpenAITranscriptionAdapter.DEFAULT_MODEL) for consistency rather than the
# realtime-specific whisper-1/gpt-realtime-whisper options that also appear in the docs.
_TRANSCRIPTION_MODEL = "gpt-transcribe"


def _strip_cache_boundary(instructions: str) -> str:
    return instructions.replace(_CACHE_BOUNDARY, "")


def _flatten_usage(usage: dict) -> Dict[str, int]:
    """Flattens OpenAI's real response.done usage object into the flat leg
    keys VoiceCallBuffer.add_usage / domain.billing.calculate_realtime_cost
    expect (Task 16, RFC §4.12/§6).

    OpenAI's Realtime API reports usage nested — roughly
    ``{"input_tokens", "output_tokens",
    "input_token_details": {"text_tokens", "audio_tokens", "cached_tokens",
    "cached_tokens_details": {"text_tokens", "audio_tokens"}},
    "output_token_details": {"text_tokens", "audio_tokens"}}`` — NOT flat
    ``audio_input_tokens``/etc keys (verified against developers.openai.com's
    live reference plus community-reported real payloads, checked
    2026-09-21; this repo's own Phase 0 spike never exercised OpenAI's usage
    shape - see the adapter's other "verified live, not against spike data"
    comments). Passing the raw nested dict through unflattened would make
    ``VoiceCallBuffer.add_usage(model, **usage)`` set a bucket key to a
    nested dict, breaking its ``bucket.get(key, 0) + value`` arithmetic
    (``0 + {...}`` raises ``TypeError``) the first time a real call reports
    a `*_token_details` breakdown.

    ``cached_tokens`` is a SUBSET of ``input_token_details``'s
    ``text_tokens``/``audio_tokens`` counts, not additive (OpenAI's own
    documented example: ``text_tokens=119`` includes the ``cached_tokens=64``
    reported alongside it) — so it is subtracted from the audio/text input
    legs here, matching this codebase's existing convention that
    ``prompt_tokens`` is always uncached input and cache reads are billed as
    a separate leg (see CLAUDE.md: "prompt_tokens in UsageMetadata always =
    uncached input").

    Reasoning tokens bill as text output (RFC §4.3/§6). The Realtime API does
    not currently surface them as their own visible field (folded into
    ``output_token_details.text_tokens`` per OpenAI's community-reported
    behaviour) — this still adds ``output_token_details.reasoning_tokens``
    onto ``text_output_tokens`` in case a future API revision does add one,
    rather than silently dropping it.

    Some realtime deployments only return the undifferentiated top-level
    ``input_tokens``/``output_tokens`` totals with no ``*_token_details`` at
    all (community-reported). There is no way to split an undifferentiated
    total into audio/text legs, so this passes ``usage`` through unchanged in
    that case rather than inventing a split — every value in it is already a
    plain int, so add_usage's arithmetic stays safe.
    """
    input_details = usage.get("input_token_details")
    output_details = usage.get("output_token_details")
    if input_details is None and output_details is None:
        return usage

    input_details = input_details or {}
    output_details = output_details or {}
    cached_details = input_details.get("cached_tokens_details") or {}
    cached_tokens = input_details.get("cached_tokens", 0)

    if cached_details:
        cached_audio_tokens = cached_details.get("audio_tokens", 0)
        cached_text_tokens = cached_details.get("text_tokens", 0)
    else:
        # No per-modality split for the cached subset - the dominant realtime
        # caching case is cached system instructions/tools (text), so treat
        # the whole cached count as text rather than leaving it inside
        # audio_input_tokens where it would be double-billed at both the full
        # audio rate and the cached rate.
        cached_audio_tokens = 0
        cached_text_tokens = cached_tokens

    return {
        "audio_input_tokens": max(input_details.get("audio_tokens", 0) - cached_audio_tokens, 0),
        "audio_output_tokens": output_details.get("audio_tokens", 0),
        "text_input_tokens": max(input_details.get("text_tokens", 0) - cached_text_tokens, 0),
        "text_output_tokens": output_details.get("text_tokens", 0) + output_details.get("reasoning_tokens", 0),
        "cached_tokens": cached_tokens,
    }


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
                # "transcription" enables conversation.item.input_audio_transcription.completed -
                # without it OpenAI never emits that event and user_transcript in _normalize()
                # below is unreachable, shipping every VoiceTurnSegment.request_text empty.
                # Field shape verified against developers.openai.com's live GA client-events
                # reference and realtime-conversations guide (checked 2026-09-21), not inferred
                # from this repo's Phase 0 spike data.
                "input": {"format": {"type": "audio/pcmu"}, "transcription": {"model": _TRANSCRIPTION_MODEL}},
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
            return RealtimeSessionEvent(
                type="response_done", payload={"usage": _flatten_usage(usage), "model": self._model}
            )
        if event_type == "response.created":
            return RealtimeSessionEvent(type="response_created", payload={})
        # Event names verified against developers.openai.com's live GA Realtime API docs
        # (client-events reference + realtime-conversations guide, checked 2026-09-21) - NOT
        # against this repo's voice_spike_01 data, which only traced xAI's leg through these
        # audio-transcript events; OpenAI's leg in that spike used text-modality events
        # throughout and never exercised either event below.
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
