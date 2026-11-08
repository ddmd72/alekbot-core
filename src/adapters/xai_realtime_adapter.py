import asyncio
import json
from collections import deque
from typing import Any, AsyncIterator, Callable, Deque, Dict, List, Optional
from urllib.parse import quote

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from src.domain.billing import REALTIME_WEB_SEARCH_CALLS, REALTIME_X_SEARCH_CALLS
from src.domain.voice_audio_format import MULAW_8K, AudioFormat
from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_turn_ownership import TurnOwnership
from src.ports.realtime_session_port import RealtimeSessionEvent, RealtimeSessionPort
from src.utils.logger import logger

_MODEL = "grok-voice-3.0"
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
# xAI's server-side search tools (live probe 2026-11-08, grok-voice-3.0), offered when the call's spec switches them
# on (VoiceSessionSpec.builtin_search): declared bare in session.tools, run by xAI itself, and reported back as
# ordinary function_call items named after the tool, at the end of the response. The client never answers them: no
# tool_call event, no function_call_output. Their calls are filtered and counted whether or not they were offered.
_BUILTIN_TOOLS = ({"type": "web_search"}, {"type": "x_search"})
# Built-in tool name -> the per-call usage leg its calls are counted in (priced in domain.billing).
_BUILTIN_TOOL_LEGS = {"web_search": REALTIME_WEB_SEARCH_CALLS, "x_search": REALTIME_X_SEARCH_CALLS}


# Session resumption (live probe P3, 2026-11-08; VOICE_CALL_CONTROL_CHANNEL_RFC §6.1): `resumption.enabled` is sent on
# every connection, a resume adds `conversation_id=<id>` to the URL, and the server replays the earlier items as
# `conversation.item.added` with `replayed: true` BEFORE `session.updated`. A bogus or expired id gets no error: the
# session opens fresh, so "resumed" means "a replayed item arrived before session.updated".
_RESUME_WAIT_S = 5.0


def _is_replayed(event: dict) -> bool:
    item = event.get("item")
    return event.get("type") == "conversation.item.added" and bool(
        event.get("replayed") or (item.get("replayed") if isinstance(item, dict) else False))


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

    The token legs are per response, but `billable_audio_seconds` is the session's
    running total (live probe 2026-10-13: 7 → 14 → 23 s over three replies). This
    function passes it through raw; the adapter turns it into a per-response delta.
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


def _builtin_tool_calls(response: dict) -> Dict[str, int]:
    """Calls of xAI's built-in tools in one response, as per-call usage legs.

    Counted from `response.done`'s `response.output`, where each search is a function_call item
    named after the tool; `usage` has no counter of its own. A leg is present only when non-zero.
    """
    counts: Dict[str, int] = {}
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        leg = _BUILTIN_TOOL_LEGS.get(item.get("name"))
        if leg:
            counts[leg] = counts.get(leg, 0) + 1
    return counts


class XaiRealtimeAdapter(RealtimeSessionPort):
    """RealtimeSessionPort against xAI's Voice Agent API (OpenAI-GA-compatible events;
    probed live 2026-09-29, decisions/voice_xai_protocol_probe.md). Provider-owned turns:
    xAI starts every reply to the caller and stops it when talked over
    (VOICE_MULTI_PROVIDER_RFC §4.3/§4.4)."""

    supported_turn_ownership = frozenset({TurnOwnership.PROVIDER})

    def __init__(self, api_key: str, model: str = _MODEL, voice: str = _VOICE,
                 ws_connect: Callable = websockets.connect, audio_format: AudioFormat = MULAW_8K,
                 builtin_search: bool = False) -> None:
        """builtin_search: offer xAI's own web_search and x_search in the session."""
        self._api_key = api_key
        self._builtin_search = builtin_search
        self._model = model
        self._voice = voice
        self._connect = ws_connect
        self._audio_format = audio_format
        self._ws: Optional[ClientConnection] = None
        # Highest running total of billable_audio_seconds seen this session. One adapter
        # per call (relay_main), so this is the call's billed time so far.
        self._billed_audio_seconds: float = 0
        # Events read while open() waited for session.updated on a resume; receive_events yields them first.
        self._early: Deque[RealtimeSessionEvent] = deque()
        self._resumed = False

    @property
    def _socket(self) -> ClientConnection:
        if self._ws is None:
            raise RuntimeError("realtime session is not open")
        return self._ws

    @property
    def resumed(self) -> bool:
        return self._resumed

    async def open(self, instructions: str, reasoning_effort: str, tools: List[dict],
                   resume_handle: Optional[str] = None) -> None:
        url = f"wss://api.x.ai/v1/realtime?model={self._model}"
        if resume_handle:
            url += f"&conversation_id={quote(resume_handle, safe='')}"
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
            "resumption": {"enabled": True},
        }
        clashing = sorted(tool["name"] for tool in tools if tool.get("name") in _BUILTIN_TOOL_LEGS)
        if clashing:
            # Calls of a function named after a built-in are taken for xAI's own and never answered.
            raise ValueError(f"function tools named like xAI built-in tools: {clashing}")
        # Built-ins go bare; our own tools are functions. tool_choice is sent even with built-ins alone.
        builtins = list(_BUILTIN_TOOLS) if self._builtin_search else []
        if builtins or tools:
            session["tools"] = [*builtins, *({"type": "function", **tool} for tool in tools)]
            session["tool_choice"] = "auto"
        await self._socket.send(json.dumps({"type": "session.update", "session": session}))
        if resume_handle:
            await self._read_until_session_updated()

    async def _read_until_session_updated(self) -> None:
        """A resume: read up to session.updated to learn whether the server honoured the id (it replays items
        before that event; an unknown or expired id replays nothing and raises no error). The events read are kept
        for receive_events, replayed items excepted: they are history, not new turns."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RESUME_WAIT_S
        replayed = False
        while True:
            try:
                raw = await asyncio.wait_for(self._socket.recv(), max(deadline - loop.time(), 0.01))
            except (asyncio.TimeoutError, ConnectionClosed):
                break
            event = json.loads(raw)
            if _is_replayed(event):
                replayed = True
                continue
            normalized = self._normalize(event)
            if normalized is not None:
                self._early.append(normalized)
            if event.get("type") == "session.updated":
                break
        self._resumed = replayed
        if not replayed:
            logger.warning("xAI realtime resume: not honoured, the call opens fresh")

    async def send_audio(self, frame: AudioFrame) -> None:
        payload = frame.payload if isinstance(frame.payload, str) else frame.payload.decode("ascii")
        await self._socket.send(json.dumps({"type": "input_audio_buffer.append", "audio": payload}))

    async def receive_events(self) -> AsyncIterator[RealtimeSessionEvent]:
        while self._early:
            yield self._early.popleft()
        async for raw in self._socket:
            normalized = self._normalize(json.loads(raw))
            if normalized is not None:
                yield normalized

    def _normalize(self, event: dict) -> Optional[RealtimeSessionEvent]:
        event_type = event.get("type")
        if event_type in ("response.output_audio.delta", "response.audio.delta"):
            payload = event.get("delta")
            if not payload:
                return None
            frame = AudioFrame(encoding=self._audio_format.encoding,
                               sample_rate_hz=self._audio_format.sample_rate_hz,
                               payload=payload, track="outbound")
            return RealtimeSessionEvent(
                type="audio_delta", payload={"frame": frame, "item_id": event.get("item_id")}
            )
        if event_type == "response.function_call_arguments.done":
            if event.get("name") in _BUILTIN_TOOL_LEGS:
                # xAI ran it server-side; it is counted for billing on response.done.
                logger.info(f"xAI realtime built-in tool call: {event.get('name')}")
                return None
            return RealtimeSessionEvent(
                type="tool_call",
                payload={"call_id": event.get("call_id"), "name": event.get("name"), "arguments": event.get("arguments")},
            )
        if event_type == "conversation.created":
            handle = (event.get("conversation") or {}).get("id")
            if not handle:
                return None
            return RealtimeSessionEvent(type="resume_handle", payload={"handle": handle})
        if event_type == "response.created":
            return RealtimeSessionEvent(type="response_created", payload={})
        if event_type == "response.done":
            # Usage sits at the event's top level on xAI, not under `response`.
            usage = _flatten_usage(event.get("usage") or {})
            # VoiceCallBuffer sums every response_done; summing xAI's running total
            # billed a 4-minute call as 31 minutes. Hand it the increment instead.
            running_total = usage["billable_audio_seconds"]
            usage["billable_audio_seconds"] = max(running_total - self._billed_audio_seconds, 0)
            self._billed_audio_seconds = max(self._billed_audio_seconds, running_total)
            builtin_calls = _builtin_tool_calls(event.get("response") or {})
            usage.update(builtin_calls)
            logger.info(f"xAI realtime usage: running billable_audio_seconds={running_total} "
                        f"delta={usage['billable_audio_seconds']} builtin_calls={builtin_calls}")
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
        await self._socket.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": output},
        }))

    async def submit_message(self, role: str, text: str) -> None:
        await self._socket.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]},
        }))

    async def request_response(self) -> None:
        # The greeting and delegation answers: the only replies the relay starts under PROVIDER ownership.
        await self._socket.send(json.dumps({"type": "response.create"}))

    async def cancel_response(self) -> None:
        # Not called under PROVIDER ownership (xAI stops its own reply); kept for the port contract.
        await self._socket.send(json.dumps({"type": "response.cancel"}))

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        await self._socket.send(json.dumps({
            "type": "conversation.item.truncate",
            "item_id": item_id,
            "content_index": 0,
            "audio_end_ms": audio_end_ms,
        }))

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
