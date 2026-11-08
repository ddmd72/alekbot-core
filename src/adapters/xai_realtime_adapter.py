import asyncio
import json
import time
from collections import deque
from typing import Any, AsyncIterator, Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

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
# Remote MCP calls: xAI runs them itself, and the response that made them ends (`response.done`) before they finish.
# Nothing speaks the results until a `response.create`: the server starts no follow-up response on its own. The adapter
# reports each finished call (`remote_tool_done`) and, on `response_done`, which of the response's calls still run; the
# session loop sends the `response.create`.
# xAI's wire (live probe with a public no-auth MCP server, grok-voice-3.0): every event names the call by `call_id`;
# the call's items are `function_call` items named `<server_label>.<tool>` (never an `mcp_call` item, no
# `server_label` field). In order: `response.mcp_call_arguments.delta`/`.done` (item_id, call_id, name),
# `response.mcp_call.in_progress` (call_id, name), `response.output_item.added` + `conversation.item.added` (the
# function_call item, in_progress), `response.done` (the function_call items, before any result), then per call
# `conversation.item.added` of a `function_call_output` item (call_id, output) and `response.mcp_call.completed`
# (call_id, name, output). Parallel calls interleave the same way. The OpenAI Realtime MCP shapes (`mcp_call` items
# keyed by item id) are accepted as aliases. A call counts as finished on the first of: a completed/failed event, its
# function_call_output item, an `mcp_call` item carrying its output, error or a final status; it is reported once.
_MCP_CALL = "mcp_call"
_FUNCTION_CALL = "function_call"
_FUNCTION_CALL_OUTPUT = "function_call_output"
_MCP_FINISHED_EVENTS = {"response.mcp_call.completed": True, "response.mcp_call.failed": False}
_MCP_STARTED_EVENTS = frozenset({"response.mcp_call.in_progress", "response.mcp_call_arguments.done"})
_OUTPUT_ITEM_EVENTS = frozenset({"response.output_item.added", "response.output_item.done"})
_MCP_ITEM_EVENTS = frozenset({*_ITEM_EVENTS, *_OUTPUT_ITEM_EVENTS})
_MCP_FINAL_STATUSES = frozenset({"completed", "failed", "incomplete"})
# Item types the adapter knows; any other in a response's output is logged by its type (a renamed shape shows up).
_KNOWN_ITEM_TYPES = frozenset({"message", _FUNCTION_CALL, _FUNCTION_CALL_OUTPUT, _MCP_CALL, "mcp_list_tools"})
# A call duration the provider may report itself; when it reports none, the adapter measures from the call's first event.
_MCP_REPORTED_MS_KEYS = ("duration_ms", "latency_ms")


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


def _event_item(event: dict) -> Optional[dict]:
    item = event.get("item")
    return item if isinstance(item, dict) else None


def _call_key(source: dict, id_field: str) -> Optional[str]:
    """A call's key: xAI's `call_id`, else the OpenAI-style item id (`item_id` on events, `id` on items)."""
    for candidate in (source.get("call_id"), source.get(id_field)):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _mcp_item_finished(item: dict) -> Optional[bool]:
    """True/False (succeeded/failed) for an mcp_call item that carries its result, None while it still runs."""
    if item.get("error"):
        return False
    if item.get("output") is not None:
        return True
    status = item.get("status")
    if status in _MCP_FINAL_STATUSES:
        return status == "completed"
    return None


def _reported_ms(*sources: Optional[dict]) -> Optional[int]:
    for source in sources:
        for key in _MCP_REPORTED_MS_KEYS:
            value = (source or {}).get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return round(value)
    return None


class XaiRealtimeAdapter(RealtimeSessionPort):
    """RealtimeSessionPort against xAI's Voice Agent API (OpenAI-GA-compatible events;
    probed live 2026-09-29, decisions/voice_xai_protocol_probe.md). Provider-owned turns:
    xAI starts every reply to the caller and stops it when talked over
    (VOICE_MULTI_PROVIDER_RFC §4.3/§4.4)."""

    supported_turn_ownership = frozenset({TurnOwnership.PROVIDER})
    # Live probe: after the caller spoke since the call, Grok repeated its last line over a late answer sent as a system
    # note and spoke the same answer sent as the call's function_call_output (VOICE_COMPANION_RFC §4.7 amendment).
    late_tool_result_after_caller_turn = True

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
        # The call's exact tokens, held only to scrub them by value from provider text before it is logged.
        self._secrets: Tuple[str, ...] = ()
        # Events read while open() waited for session.updated on a resume; receive_events yields them first.
        self._early: Deque[RealtimeSessionEvent] = deque()
        self._resumed = False
        # MCP calls by item id: when the adapter first saw each (for the call's duration), its server label, and the
        # calls already reported finished (each is reported once, whichever event shows it first).
        self._mcp_seen: Dict[str, float] = {}
        self._mcp_labels: Dict[str, str] = {}
        self._mcp_finished: Set[str] = set()
        # Calls first seen since the last response.created: response.done reports them even when its output does not
        # show them as MCP calls (the safety net for a shape this adapter does not know yet).
        self._response_calls: List[str] = []

    @property
    def _socket(self) -> ClientConnection:
        if self._ws is None:
            raise RuntimeError("realtime session is not open")
        return self._ws

    @property
    def resumed(self) -> bool:
        return self._resumed

    async def open(self, instructions: str, reasoning_effort: str, tools: List[dict],
                   resume_handle: Optional[str] = None, mcp_servers: Sequence[RemoteToolServer] = ()) -> None:
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
        # Built-ins go bare; our own tools are functions; remote MCP servers are `mcp` entries. tool_choice is sent
        # even with built-ins alone. Nothing here is logged: the mcp entries carry the call's bearer tokens.
        builtins = list(_BUILTIN_TOOLS) if self._builtin_search else []
        self._server_labels = frozenset(server.label for server in mcp_servers)
        self._secrets = tuple(server.token for server in mcp_servers if server.token)
        if builtins or tools or mcp_servers:
            session["tools"] = [*builtins, *({"type": "function", **tool} for tool in tools),
                                *(_mcp_entry(server) for server in mcp_servers)]
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
            if self._is_mcp_call(event.get("name"), _call_key(event, "item_id")):
                # A remote MCP call xAI runs itself: never a tool call for the relay to answer.
                self._note_mcp_call(_call_key(event, "item_id"), event)
                return None
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
            self._response_calls = []
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
            payload: Dict[str, Any] = {"usage": usage, "model": self._model}
            remote_tools = self._remote_tools_of(event.get("response") or {})
            if remote_tools is not None:
                payload["remote_tools"] = remote_tools
            return RealtimeSessionEvent(type="response_done", payload=payload)
        if event_type == "conversation.item.input_audio_transcription.completed":
            return RealtimeSessionEvent(type="user_transcript", payload={"text": event.get("transcript", "")})
        if event_type == "response.output_audio_transcript.done":
            return RealtimeSessionEvent(type="model_transcript", payload={"text": event.get("transcript", "")})
        if event_type == "input_audio_buffer.committed":
            return RealtimeSessionEvent(type="turn_committed", payload={"item_id": event.get("item_id")})
        # item_id names the caller's turn this speech becomes once committed: the "Ask Lelik" latch marks that turn
        # (VOICE_CALL_CONTROL_CHANNEL_RFC §5.4).
        if event_type == "input_audio_buffer.speech_started":
            return RealtimeSessionEvent(type="speech_started", payload={"item_id": event.get("item_id")})
        if event_type == "input_audio_buffer.speech_stopped":
            return RealtimeSessionEvent(type="speech_stopped", payload={"item_id": event.get("item_id")})
        if event_type in _LIST_TOOLS_EVENTS:
            return self._list_tools_event(event_type, event)
        if event_type in _MCP_FINISHED_EVENTS:
            return self._mcp_call_finished(_call_key(event, "item_id"), _MCP_FINISHED_EVENTS[event_type], event)
        if event_type in _MCP_STARTED_EVENTS:
            self._note_mcp_call(_call_key(event, "item_id"), event)
            return None
        item = _event_item(event) if event_type in _MCP_ITEM_EVENTS else None
        if item is not None:
            if event_type in _OUTPUT_ITEM_EVENTS:
                self._log_unknown_item_type(item)
            done = self._mcp_item_event(item, event)
            if event_type in _ITEM_EVENTS:
                self._remember_list_item(item)
            return done
        if event_type == "error":
            # xAI may echo what it was sent (the session payload with the mcp entries' tokens): redacted, never raw.
            message = redact_for_log(event.get("error", event), secrets=self._secrets)
            logger.error(f"xAI realtime error event: {message}")
            return RealtimeSessionEvent(type="error", payload={"message": message})
        if isinstance(event_type, str) and "mcp" in event_type and not event_type.endswith(".delta"):
            # An MCP event this adapter does not read: its type only, so a renamed completion event shows in the log.
            logger.info(f"xAI realtime MCP event not read: {event_type}")
        # Everything else is not needed by the session loop: dropped, never logged.
        return None

    def _mcp_label_of_name(self, name: Any) -> Optional[str]:
        """xAI names an MCP call `<server_label>.<tool>`: the label when it is one of the session's, else None."""
        if not isinstance(name, str) or "." not in name:
            return None
        label = name.split(".", 1)[0]
        return label if label in self._server_labels else None

    def _is_mcp_call(self, name: Any, key: Optional[str]) -> bool:
        return self._mcp_label_of_name(name) is not None or (key is not None and key in self._mcp_seen)

    def _is_mcp_item(self, item: dict) -> bool:
        if item.get("type") == _MCP_CALL:
            return True
        return item.get("type") == _FUNCTION_CALL and self._is_mcp_call(item.get("name"), _call_key(item, "id"))

    def _log_unknown_item_type(self, item: dict) -> None:
        item_type = item.get("type")
        if item_type not in _KNOWN_ITEM_TYPES:
            logger.info(f"xAI realtime output item type not read: {redact_for_log(str(item_type))[:40]}")

    def _mcp_item_event(self, item: dict, event: dict) -> Optional[RealtimeSessionEvent]:
        """An item event: an MCP call's item notes the call (and finishes an OpenAI-style `mcp_call` item that carries
        its result); a `function_call_output` item of a known MCP call is its result: the call has finished."""
        if self._is_mcp_item(item):
            key = _call_key(item, "id")
            self._note_mcp_call(key, event)
            finished = _mcp_item_finished(item) if item.get("type") == _MCP_CALL else None
            return None if finished is None else self._mcp_call_finished(key, finished, event)
        if item.get("type") == _FUNCTION_CALL_OUTPUT:
            key = _call_key(item, "id")
            if key is not None and key in self._mcp_seen:
                return self._mcp_call_finished(key, not item.get("error"), event)
        return None

    def _note_mcp_call(self, key: Optional[str], event: dict) -> None:
        if key is None:
            return
        if key not in self._mcp_seen:
            self._mcp_seen[key] = time.monotonic()
            self._response_calls.append(key)
        label = self._server_label(event)
        if label != _UNKNOWN_SERVER:
            self._mcp_labels.setdefault(key, label)

    def _mcp_call_finished(self, key: Optional[str], succeeded: bool, event: dict) -> Optional[RealtimeSessionEvent]:
        """One `remote_tool_done` per call, with one INFO line: the server label and the duration, never a payload."""
        if key is None or key in self._mcp_finished:
            return None
        self._note_mcp_call(key, event)
        self._mcp_finished.add(key)
        label = self._mcp_labels.get(key, _UNKNOWN_SERVER)
        reported = _reported_ms(event, _event_item(event))
        if reported is not None:
            duration = f"ms={reported} (xai)"
        else:
            duration = f"ms={round((time.monotonic() - self._mcp_seen[key]) * 1000)} (since first event)"
        logger.info(f"xAI realtime mcp call completed: server={label} {duration} ok={succeeded}")
        return RealtimeSessionEvent(type="remote_tool_done", payload={"item_id": key, "label": label, "ok": succeeded})

    def _remote_tools_of(self, response: dict) -> Optional[Dict[str, Any]]:
        """The response's MCP calls for `response_done`, None when it made none: `pending` - keys of calls not yet
        finished; `finished` - labels of calls first seen finished here (reported only in this payload);
        `continued` - the response went on to a message after its last call (it already spoke the results);
        `cancelled` - the response was cut (the caller talked over it). The calls are those its output shows plus
        those first seen since its response.created."""
        output = [item for item in response.get("output") or [] if isinstance(item, dict)]
        for item in output:
            self._log_unknown_item_type(item)
        calls = [(index, item) for index, item in enumerate(output) if self._is_mcp_item(item)]
        keys: List[str] = []
        finished: List[str] = []
        for _, item in calls:
            key = _call_key(item, "id")
            if key is None or key in keys:
                continue
            keys.append(key)
            self._note_mcp_call(key, {"item": item})
            state = _mcp_item_finished(item) if item.get("type") == _MCP_CALL else None
            if state is not None and self._mcp_call_finished(key, state, {"item": item}) is not None:
                finished.append(self._mcp_labels.get(key, _UNKNOWN_SERVER))
        keys += [key for key in self._response_calls if key not in keys]
        self._response_calls = []
        if not keys:
            return None
        continued = bool(calls) and any(item.get("type") == "message" for item in output[calls[-1][0] + 1:])
        return {"calls": len(keys), "pending": [key for key in keys if key not in self._mcp_finished],
                "finished": finished, "continued": continued, "cancelled": response.get("status") == "cancelled"}

    def _server_label(self, event: dict) -> str:
        """The event's server label when it is one of the session's, else "unknown": a label the provider made up or
        echoed from elsewhere never reaches an event or a log line."""
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        for candidate in (event.get("server_label"), item.get("server_label"), self._list_items.get(event.get("item_id")),
                          self._mcp_label_of_name(event.get("name")), self._mcp_label_of_name(item.get("name"))):
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
            reason = redact_for_log((error.get("message") if isinstance(error, dict) else error) or "no error text",
                                    secrets=self._secrets)
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
