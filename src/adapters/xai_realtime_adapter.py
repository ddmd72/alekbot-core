import json
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence

import websockets
from websockets.asyncio.client import ClientConnection

from src.domain.billing import REALTIME_WEB_SEARCH_CALLS, REALTIME_X_SEARCH_CALLS
from src.domain.log_redaction import redact_for_log
from src.domain.remote_tool_server import RemoteToolServer
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
# Remote MCP servers (LELIK_VOICE_MCP_RFC §4; live probe phase A, 2026-11-08). session.update takes one `mcp` entry per
# server next to the function tools; `authorization` goes raw (xAI adds `Bearer ` itself and does not double an
# existing prefix); session.updated echoes the entries without it. xAI calls tools/list once per server, at
# session.update and before session.updated, and never again. A failed list (a 401 among others) arrives only as
# `mcp_list_tools.failed` with an opaque error: no `error` event, and the session goes on without that server's tools.
_LIST_TOOLS_FAILED = "mcp_list_tools.failed"
_LIST_TOOLS_COMPLETED = "mcp_list_tools.completed"
_LIST_TOOLS_EVENTS = frozenset({_LIST_TOOLS_FAILED, _LIST_TOOLS_COMPLETED, "mcp_list_tools.in_progress"})
_ITEM_EVENTS = frozenset({"conversation.item.added", "conversation.item.created", "conversation.item.done"})
_UNKNOWN_SERVER = "unknown"


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


def _mcp_entry(server: RemoteToolServer) -> Dict[str, Any]:
    return {
        "type": "mcp",
        "server_url": server.url,
        "server_label": server.label,
        "server_description": server.description,
        "allowed_tools": list(server.allowed_tools),
        "authorization": server.token,
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
        # The labels of the session's MCP servers: the only server names an event or a log line may carry.
        self._server_labels: frozenset = frozenset()
        # item_id of an mcp_list_tools conversation item -> its server label, for list events that name the item only.
        self._list_items: Dict[str, str] = {}

    @property
    def _socket(self) -> ClientConnection:
        if self._ws is None:
            raise RuntimeError("realtime session is not open")
        return self._ws

    async def open(self, instructions: str, reasoning_effort: str, tools: List[dict],
                   mcp_servers: Sequence[RemoteToolServer] = ()) -> None:
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
        clashing = sorted(tool["name"] for tool in tools if tool.get("name") in _BUILTIN_TOOL_LEGS)
        if clashing:
            # Calls of a function named after a built-in are taken for xAI's own and never answered.
            raise ValueError(f"function tools named like xAI built-in tools: {clashing}")
        # Built-ins go bare; our own tools are functions; remote MCP servers are `mcp` entries. tool_choice is sent
        # even with built-ins alone. Nothing here is logged: the mcp entries carry the call's bearer tokens.
        builtins = list(_BUILTIN_TOOLS) if self._builtin_search else []
        self._server_labels = frozenset(server.label for server in mcp_servers)
        if builtins or tools or mcp_servers:
            session["tools"] = [*builtins, *({"type": "function", **tool} for tool in tools),
                                *(_mcp_entry(server) for server in mcp_servers)]
            session["tool_choice"] = "auto"
        await self._socket.send(json.dumps({"type": "session.update", "session": session}))

    async def send_audio(self, frame: AudioFrame) -> None:
        payload = frame.payload if isinstance(frame.payload, str) else frame.payload.decode("ascii")
        await self._socket.send(json.dumps({"type": "input_audio_buffer.append", "audio": payload}))

    async def receive_events(self) -> AsyncIterator[RealtimeSessionEvent]:
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
        if event_type in _LIST_TOOLS_EVENTS:
            return self._list_tools_event(event_type, event)
        if event_type in _ITEM_EVENTS:
            self._remember_list_item(event.get("item"))
            return None
        if event_type == "error":
            # xAI may echo what it was sent (the session payload with the mcp entries' tokens): redacted, never raw.
            message = redact_for_log(event.get("error", event))
            logger.error(f"xAI realtime error event: {message}")
            return RealtimeSessionEvent(type="error", payload={"message": message})
        # Everything else, response.mcp_call.* included, is not needed by the session loop: dropped, never logged.
        return None

    def _server_label(self, event: dict) -> str:
        """The event's server label when it is one of the session's, else "unknown": a label the provider made up or
        echoed from elsewhere never reaches an event or a log line."""
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        for candidate in (event.get("server_label"), item.get("server_label"), self._list_items.get(event.get("item_id"))):
            if isinstance(candidate, str) and candidate in self._server_labels:
                return candidate
        return _UNKNOWN_SERVER

    def _remember_list_item(self, item: Any) -> None:
        if isinstance(item, dict) and item.get("type") == "mcp_list_tools" and isinstance(item.get("id"), str):
            label = item.get("server_label")
            if isinstance(label, str) and label in self._server_labels:
                self._list_items[item["id"]] = label

    def _list_tools_event(self, event_type: str, event: dict) -> Optional[RealtimeSessionEvent]:
        label = self._server_label(event)
        if event_type == _LIST_TOOLS_FAILED:
            error = event.get("error")
            reason = redact_for_log((error.get("message") if isinstance(error, dict) else error) or "no error text")
            return RealtimeSessionEvent(type="remote_tools_failed", payload={"label": label, "reason": reason})
        if event_type == _LIST_TOOLS_COMPLETED:
            logger.info(f"xAI realtime MCP tools listed: server={label}")
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
