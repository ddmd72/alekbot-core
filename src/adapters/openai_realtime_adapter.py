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
# semantic_vad ends a turn on what was said, not on a silence timer, so a mid-thought
# pause does not hand Lelik the floor; "low" waits longest. interrupt_response=False
# leaves barge-in to VoiceSessionService alone: with the provider also auto-cancelling,
# our own response.cancel could land on nothing and the provider's error ends the call.
# Shape: session.audio.input.turn_detection (developers.openai.com realtime-vad guide +
# client-events reference, checked 2026-09-22). idle_timeout_ms is server_vad-only, so
# silence is detected relay-side (VoiceSessionService's watchdog).
# One voice for every call until it becomes a per-user setting. Owner's pick 2026-09-25
# (was cedar: flat intonation on live calls). Fixed per session once audio is emitted.
_VOICE = "ballad"
# Post-generation speed multiplier (0.25-1.5). 1.15 was tried and audibly degraded the audio
# (line-noise artefacts over 8 kHz telephony), so it stays at the provider default.
_SPEED = 1.0
_TURN_DETECTION = {
    "type": "semantic_vad",
    "eagerness": "low",
    # The relay starts every reply itself (VoiceSessionService, on `turn_committed`) so
    # it can put the persona anchor right after the caller's turn first.
    "create_response": False,
    "interrupt_response": False,
}
# The billing-leg keys calculate_realtime_cost/VoiceCallBuffer.add_usage recognize -
# used by _flatten_usage to detect when the undifferentiated-totals fallback path
# produced a dict none of them price (see the warning below).
_KNOWN_BILLING_LEG_KEYS = frozenset(
    {"audio_input_tokens", "audio_output_tokens", "text_input_tokens", "text_output_tokens", "cached_tokens"}
)


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
    ``audio_input_tokens``/etc keys. **This shape is inferred from
    secondary/community-reported sources only** — the live
    developers.openai.com reference page returned a 403 on every direct
    fetch attempt during this investigation (checked 2026-09-21) and could
    not be consulted directly; this repo's own Phase 0 spike never
    exercised OpenAI's usage shape either - see the adapter's other
    "verified live, not against spike data" comments. Treat this shape as
    unconfirmed until checked against a captured real payload. Passing the
    raw nested dict through unflattened would make
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

    Reasoning tokens bill as text output (RFC §4.3/§6), and they are a SUBSET
    of ``output_tokens`` — and therefore of ``output_token_details.text_tokens``
    — never an extra quantity charged on top of it. OpenAI's reasoning guide
    states this directly: reasoning tokens "are billed as output tokens", i.e.
    counted once, inside the output total. This repo's own Phase 0 spike uses
    the same convention independently: ``scripts/voice/test_reasoning_effort_poc.py``
    sums top-level ``usage["output_tokens"]`` for its cost figure and tracks
    ``output_token_details.reasoning_tokens`` separately purely for display,
    never adding it in (see also
    ``decisions/voice_spike_04_reasoning_effort.md``, whose cost formula is
    ``input_tokens*4 + output_tokens*24`` with no reasoning term). So
    ``text_output_tokens`` is ``output_token_details.text_tokens`` alone:
    adding ``reasoning_tokens`` onto it would double-bill the $24/1M
    output-text leg on every call that reports a non-zero split.

    Some realtime deployments only return the undifferentiated top-level
    ``input_tokens``/``output_tokens`` totals with no ``*_token_details`` at
    all (community-reported). There is no way to split an undifferentiated
    total into audio/text legs, so this passes ``usage`` through unchanged in
    that case rather than inventing a split — every value in it is already a
    plain int, so add_usage's arithmetic stays safe. Because none of those
    top-level keys match a leg ``calculate_realtime_cost`` prices, this
    silently costs $0.00 for that call — a ``logger.warning`` fires when this
    fallback is taken on a non-empty usage dict, so a real occurrence is
    visible in Cloud Run logs instead of silently under-billing.
    """
    input_details = usage.get("input_token_details")
    output_details = usage.get("output_token_details")
    if input_details is None and output_details is None:
        if usage and _KNOWN_BILLING_LEG_KEYS.isdisjoint(usage):
            logger.warning(
                "OpenAI realtime usage arrived in an unrecognized shape - no "
                "input_token_details/output_token_details and none of the known "
                f"billing-leg keys {sorted(_KNOWN_BILLING_LEG_KEYS)} are present. "
                f"Passing it through unchanged means calculate_realtime_cost will "
                f"price this call at $0.00. Raw usage: {usage!r}"
            )
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
        "text_output_tokens": output_details.get("text_tokens", 0),
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
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "transcription": {"model": _TRANSCRIPTION_MODEL},
                    "turn_detection": _TURN_DETECTION,
                },
                "output": {"format": {"type": "audio/pcmu"}, "voice": _VOICE, "speed": _SPEED},
            },
            "reasoning": {"effort": reasoning_effort},
            "instructions": _strip_cache_boundary(instructions),
        }
        if tools:
            # The port carries the neutral declaration (BaseAgent._build_delegate_tool_declaration);
            # OpenAI Realtime wants type=function.
            session["tools"] = [{"type": "function", **tool} for tool in tools]
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
            return RealtimeSessionEvent(
                type="audio_delta", payload={"frame": frame, "item_id": event.get("item_id")}
            )
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
        if event_type == "input_audio_buffer.committed":
            return RealtimeSessionEvent(type="turn_committed", payload={"item_id": event.get("item_id")})
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

    async def cancel_response(self) -> None:
        # Barge-in: stop the provider generating the response the caller just talked
        # over (validated live in scripts/voice/test_mulaw_relay_poc.py:158-170). The
        # caller must only reach here while a response is actually active - OpenAI
        # errors on a response.cancel with nothing in flight.
        await self._ws.send(json.dumps({"type": "response.cancel"}))

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        # Over WebSocket the server cannot know what was played, so the client must cut
        # the unheard tail itself (realtime-conversations guide, checked 2026-09-22).
        await self._ws.send(json.dumps({
            "type": "conversation.item.truncate",
            "item_id": item_id,
            "content_index": 0,
            "audio_end_ms": audio_end_ms,
        }))

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
