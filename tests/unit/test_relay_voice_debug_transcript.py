"""VOICE_DEBUG_TRANSCRIPT_USERS (debug only, docs/07_deployment/README.md relay knob 8): a `;`-separated list of user
ids read at relay boot; a listed user's calls log `voice debug` lines with events and speech text, in any mode. Text
never reaches a line of an unlisted user's call, and the boot line names no id."""
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional

import pytest
import yaml

import relay_main
from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_control_protocol import control_token_digest
from src.domain.voice_playback_tracker import PlaybackTracker
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.domain.voice_relay_call_options import VoiceRelayCallOptions
from src.domain.voice_turn_ownership import TurnOwnership
from src.handlers.call_control_handler import CallControlHandler
from src.ports.realtime_session_port import RealtimeSessionEvent, RealtimeSessionPort
from src.services.call_control_hub import CallControlHub
from src.services.voice_session_service import VoiceSessionService
from tests.support.control_socket_fake import FakeControlSocket

_KNOB = "VOICE_DEBUG_TRANSCRIPT_USERS"
TOKEN = "token-0003-invented"
TICKET = "ticket-0003-invented"
CALLER_WORDS = "placeholder words of the caller"
LELIK_WORDS = "placeholder words of the reply"
PAIR_ARGS = json.dumps({"caller_language": "uk", "other_language": "fr"})
_SETTLE_S = 0.03
_ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ the knob


@pytest.mark.parametrize(("raw", "expected"), [
    pytest.param(None, frozenset(), id="unset"),
    pytest.param("", frozenset(), id="empty"),
    pytest.param(" ; ;", frozenset(), id="only-separators"),
    pytest.param("user-a", frozenset({"user-a"}), id="one"),
    pytest.param(" user-a ; user-b ", frozenset({"user-a", "user-b"}), id="two-with-spaces"),
])
def test_the_knob_parses_semicolon_separated_ids(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(_KNOB, raising=False)
    else:
        monkeypatch.setenv(_KNOB, raw)
    assert relay_main._voice_debug_transcript_users() == expected


def test_the_boot_line_gives_the_count_and_never_an_id(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(_KNOB, "user-a;user-b")
    relay_main._voice_debug_transcript_users()
    messages = [record.getMessage() for record in caplog.records]
    assert any("2 listed users" in message for message in messages)
    assert not any("user-a" in message or "user-b" in message for message in messages)


def test_off_logs_nothing_at_boot(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.delenv(_KNOB, raising=False)
    relay_main._voice_debug_transcript_users()
    assert not any("debug" in record.getMessage() for record in caplog.records)


# ------------------------------------------------------------------ the deploy wiring


def _relay_args() -> List[str]:
    build = yaml.safe_load((_ROOT / "cloudbuild-dev.yaml").read_text())
    (args,) = [s["args"] for s in build["steps"] if s.get("args", [])[:3] == ["run", "deploy", "alek-voice-relay-dev"]]
    return args


def test_the_relay_deploy_sets_the_knob_from_its_substitution_default_empty():
    env_arg = next(a.split("=", 1)[1] for a in _relay_args() if a.startswith("--set-env-vars="))
    env = dict(pair.split("=", 1) for pair in env_arg.split(","))
    assert env[_KNOB] == "$_VOICE_DEBUG_TRANSCRIPT_USERS"
    build = yaml.safe_load((_ROOT / "cloudbuild-dev.yaml").read_text())
    assert build["substitutions"]["_VOICE_DEBUG_TRANSCRIPT_USERS"] == ""


def test_make_deploy_passes_the_deploy_only_key_quoted():
    makefile = (_ROOT / "Makefile").read_text()
    assert re.search(r"^VOICE_DEBUG_TRANSCRIPT_USERS_DEV \?=\s*$", makefile, re.M)
    assert "_VOICE_DEBUG_TRANSCRIPT_USERS='$(VOICE_DEBUG_TRANSCRIPT_USERS_DEV)'" in makefile


# ------------------------------------------------------------------ the call


class _Provider(RealtimeSessionPort):
    supported_turn_ownership = frozenset({TurnOwnership.PROVIDER, TurnOwnership.RELAY})

    def __init__(self) -> None:
        self.events: "asyncio.Queue[Optional[RealtimeSessionEvent]]" = asyncio.Queue()

    async def open(self, instructions: str, reasoning_effort: str, tools: List[Any]) -> None:
        pass

    async def send_audio(self, frame: AudioFrame) -> None:
        pass

    async def receive_events(self) -> AsyncIterator[RealtimeSessionEvent]:
        while True:
            event = await self.events.get()
            if event is None:
                return
            yield event

    async def update_session(self, instructions: Optional[str] = None, tools: Optional[Any] = None) -> None:
        pass

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        pass

    async def submit_message(self, role: str, text: str) -> None:
        pass

    async def request_response(self) -> None:
        pass

    async def cancel_response(self) -> None:
        pass

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        pass

    async def close(self) -> None:
        pass


class _ControlPlane:
    def __init__(self, config: dict) -> None:
        self._config = config

    async def fetch_session_config(self, ticket: str) -> dict:
        return self._config

    async def delegate(self, **kwargs: Any) -> str:
        return "placeholder answer"

    async def abandon_delegation(self, ticket: str, call_id: str) -> None:
        pass

    async def submit_transcript(self, **kwargs: Any) -> None:
        pass


class _Alerts:
    async def post(self, text: str) -> None:
        pass


async def _noop(*args: Any) -> None:
    pass


async def _run_call(user_id: str, listed: frozenset, mode: str = "interpreter") -> None:
    """One web call: a press, the caller's words to Lelik, a reply that sets the pair, the confirmation, and one more
    utterance for translation."""
    hub = CallControlHub()
    provider = _Provider()
    ends = asyncio.Event()
    control_plane = _ControlPlane({
        "instructions": "base", "user_id": user_id, "account_id": "acc-1", "tools": [], "call_kind": "web",
        "mode": mode, "voice": VOICE_PROVIDER_PROFILES["xai"].session.to_dict(),
        "control_token_sha256": control_token_digest(TOKEN)})
    service = VoiceSessionService(realtime_session_factory=lambda spec: provider, control_plane=control_plane,
                                  alert_sink=_Alerts(), control_hub=hub, ask_tick_s=0.005,
                                  call_options=VoiceRelayCallOptions(debug_transcript_users=listed))

    async def inbound():
        await ends.wait()
        return
        yield  # pragma: no cover

    page = FakeControlSocket()
    tasks = [asyncio.ensure_future(service.handle_call(
        ticket=TICKET, inbound_audio=inbound(), send_outbound_audio=_noop, clear_outbound_audio=_noop,
        playback=PlaybackTracker()))]
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    tasks.append(asyncio.ensure_future(CallControlHandler(hub, link_wait_s=1.0).handle_connection(page)))
    await asyncio.sleep(_SETTLE_S)

    async def says(event_type: str, **payload: Any) -> None:
        provider.events.put_nowait(RealtimeSessionEvent(type=event_type, payload=payload))
        await asyncio.sleep(_SETTLE_S)

    page.page_says({"type": "ask_arm", "seq": 1})
    await asyncio.sleep(_SETTLE_S)
    await says("speech_started", item_id="item-u1")
    await says("speech_stopped", item_id="item-u1")
    await says("turn_committed", item_id="item-u1")
    await says("user_transcript", item_id="item-u1", text=CALLER_WORDS)
    await says("response_created")
    await says("tool_call", call_id="call-1", name="set_language_pair", arguments=PAIR_ARGS)
    await says("response_done")
    await says("response_created")
    await says("model_transcript", text=LELIK_WORDS)
    await says("response_done")
    await says("speech_started", item_id="item-u2")
    await says("speech_stopped", item_id="item-u2")
    await says("turn_committed", item_id="item-u2")
    ends.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


def _debug_lines(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("voice debug ")]


@pytest.mark.asyncio
async def test_a_listed_users_interpreter_call_logs_events_notes_tools_and_both_sides_text(caplog):
    caplog.set_level(logging.DEBUG)
    await _run_call("user-a", frozenset({"user-a", "user-b"}))
    lines = _debug_lines(caplog)
    joined = "\n".join(lines)

    assert all(re.match(r"voice debug \S+: \+\d+ms ", line) for line in lines)
    assert "user-a" not in joined                                    # the id itself is never logged
    for expected in (
        "speech_started item-u1", "speech_stopped item-u1", "turn_committed item-u1",
        f"caller said (item-u1): {CALLER_WORDS}", f"Lelik said: {LELIK_WORDS}",
        "system note inserted: pickup", "system note inserted: ask", "system note inserted: pair",
        f"tool call call-1 set_language_pair: {PAIR_ARGS}", "tool output call-1: Tool note: pair set",
        "session updated: tools=[set_language_pair]", "session updated: tools=[]", "session updated: instructions=",
        "latch idle -> armed", "latch armed -> active", "latch awaiting_reply -> idle, reason=answered",
        "language pair set (uk→caller, fr→other)", "response_created", "response_done",
    ):
        assert expected in joined, expected


@pytest.mark.asyncio
async def test_a_listed_users_call_in_another_mode_logs_its_text_too(caplog):
    caplog.set_level(logging.DEBUG)
    await _run_call("user-a", frozenset({"user-a"}), mode="lelik")
    joined = "\n".join(_debug_lines(caplog))
    assert f"caller said (item-u1): {CALLER_WORDS}" in joined and f"Lelik said: {LELIK_WORDS}" in joined


@pytest.mark.asyncio
async def test_an_unlisted_users_call_logs_no_debug_line_and_no_text(caplog):
    caplog.set_level(logging.DEBUG)
    await _run_call("user-c", frozenset({"user-a"}))
    messages = [record.getMessage() for record in caplog.records]
    assert messages                                                   # the call did log its ordinary lines
    assert not any(message.startswith("voice debug") for message in messages)
    for text in (CALLER_WORDS, LELIK_WORDS, PAIR_ARGS):
        assert not any(text in message for message in messages), text
