"""A note inserted during a provider-owned call is followed by the persona anchor, so the note is never the freshest
system item before a reply the provider starts itself. Relay-owned calls (anchor before every reply) and calls whose
instructions build no anchor get the note alone; the start anchor stays a single item at the start."""
import asyncio
from typing import Any, List, Optional

import pytest

from src.domain.llm import build_persona_anchor
from src.domain.voice_control_protocol import control_token_digest
from src.domain.voice_playback_tracker import PlaybackTracker
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.domain.voice_turn_ownership import TurnOwnership
from src.handlers.call_control_handler import CallControlHandler
from src.ports.realtime_session_port import RealtimeSessionEvent
from src.services.call_control_hub import CallControlHub
from src.services.voice_session_service import _ASK_NOTE, VoiceControl, VoiceSessionService
from tests.support.control_socket_fake import FakeControlSocket
from tests.unit.services.test_voice_session_geo import (
    TICKET, TOKEN, _Alerts, _Clock, _ControlPlane, _geo, _noop, _Provider,
)

PERSONA = "identity {\n  You are a test persona.\n}\nspoken_delivery {\n  Speak plainly.\n}"
ANCHOR = build_persona_anchor(PERSONA)
_SETTLE_S = 0.03


class _BothProvider(_Provider):
    supported_turn_ownership = frozenset({TurnOwnership.PROVIDER, TurnOwnership.RELAY})


class _Call:
    def __init__(self, instructions: str, ownership: str = "provider", mode: Optional[str] = None) -> None:
        self.hub = CallControlHub()
        self.provider = _BothProvider()
        self.clock = _Clock()
        self.ends = asyncio.Event()
        voice = {**VOICE_PROVIDER_PROFILES["xai"].session.to_dict(), "turn_ownership": ownership}
        config = {"instructions": instructions, "user_id": "usr-1", "account_id": "acc-1", "tools": [],
                  "call_kind": "web", "voice": voice, "control_token_sha256": control_token_digest(TOKEN)}
        if mode:
            config["mode"] = mode
        self.service = VoiceSessionService(
            realtime_session_factory=lambda spec: self.provider, control_plane=_ControlPlane(config),
            alert_sink=_Alerts(), control=VoiceControl(self.hub), ask_clock=self.clock, ask_tick_s=0.005)
        self.page = FakeControlSocket()
        self._tasks: List[asyncio.Task] = []

    async def start(self) -> "_Call":
        async def inbound():
            await self.ends.wait()
            return
            yield  # pragma: no cover

        self._tasks.append(asyncio.ensure_future(self.service.handle_call(
            ticket=TICKET, inbound_audio=inbound(), send_outbound_audio=_noop, clear_outbound_audio=_noop,
            playback=PlaybackTracker())))
        self.page.page_says({"type": "hello", "v": 1, "token": TOKEN})
        self._tasks.append(asyncio.ensure_future(
            CallControlHandler(self.hub, link_wait_s=1.0).handle_connection(self.page)))
        await asyncio.sleep(_SETTLE_S)
        return self

    async def page_says(self, frame: dict) -> None:
        self.page.page_says(frame)
        await asyncio.sleep(_SETTLE_S)

    async def provider_says(self, event_type: str, **payload: Any) -> None:
        self.provider.events.put_nowait(RealtimeSessionEvent(type=event_type, payload=payload))
        await asyncio.sleep(_SETTLE_S)

    def system_items(self) -> List[str]:
        return [text for role, text in self.provider.conversation if role == "system"]

    def index_of(self, marker: str) -> int:
        return next(i for i, (_, text) in enumerate(self.provider.conversation) if marker in text)

    async def end(self) -> None:
        self.ends.set()
        await asyncio.wait_for(asyncio.gather(*self._tasks), timeout=2)


_GEO_MARKER = "The caller has just shared"


@pytest.mark.asyncio
async def test_provider_owned_geo_note_is_followed_by_the_anchor():
    assert ANCHOR is not None
    call = await _Call(PERSONA).start()
    await call.page_says(_geo())
    await call.end()

    items = call.provider.conversation
    geo_at = call.index_of(_GEO_MARKER)
    assert items[geo_at + 1] == ("system", ANCHOR)
    assert len(items) == geo_at + 2


@pytest.mark.asyncio
async def test_the_start_anchor_is_still_inserted_exactly_once_at_the_start():
    call = await _Call(PERSONA).start()
    assert call.system_items().count(ANCHOR) == 1 and call.provider.conversation[0] == ("system", ANCHOR)
    await call.page_says(_geo())
    await call.end()

    assert call.system_items().count(ANCHOR) == 2   # the start one plus the one after the geo note
    assert call.provider.conversation[0] == ("system", ANCHOR)


@pytest.mark.asyncio
async def test_relay_owned_geo_note_goes_in_alone():
    call = await _Call(PERSONA, ownership="relay").start()
    anchors_before = call.system_items().count(ANCHOR)
    await call.page_says(_geo())
    await call.end()

    items = call.provider.conversation
    geo_at = call.index_of(_GEO_MARKER)
    assert geo_at == len(items) - 1
    assert call.system_items().count(ANCHOR) == anchors_before


@pytest.mark.asyncio
async def test_provider_owned_call_without_an_anchor_gets_the_geo_note_only():
    call = await _Call("you are a plain helper").start()
    await call.page_says(_geo())
    await call.end()

    assert call.index_of(_GEO_MARKER) == len(call.provider.conversation) - 1
    assert all("PERSONALITY ANCHOR" not in text for text in call.system_items())


@pytest.mark.asyncio
async def test_provider_owned_interpreter_ask_note_is_followed_by_the_anchor():
    call = await _Call(PERSONA, mode="interpreter").start()
    await call.page_says({"type": "ask_arm", "seq": 1})
    await call.provider_says("speech_started", item_id="item-u1")
    await call.end()

    items = call.provider.conversation
    ask_at = items.index(("system", _ASK_NOTE))
    assert items[ask_at + 1] == ("system", ANCHOR)
