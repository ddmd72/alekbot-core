"""The translate note through the real VoiceSessionService (owner 2026-11-08, VOICE_CALL_CONTROL_CHANNEL_RFC §5.3): in
an interpreter call every utterance the Ask latch does not mark gets `_TRANSLATE_NOTE` on `speech_started`, placed
before the caller's turn; a pressed button gets only `_ASK_NOTE`; other modes get neither; one note per utterance;
the control channel being off changes nothing. The world (hub, `/ctl` handler, ordered provider fake) is the Ask
test module's."""
import asyncio

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.services.voice_session_service import _ASK_NOTE, _TRANSLATE_NOTE, _WEB_PICKUP_NOTE
from tests.unit.services.test_voice_session_ask_lelik import TICKET, _SETTLE_S, _Call, _noop


async def _utterance(call: _Call, item_id: str) -> None:
    await call.provider_says("speech_started", item_id=item_id)
    await call.provider_says("speech_stopped", item_id=item_id)
    await call.provider_says("turn_committed", item_id=item_id)


def test_the_note_is_bracketed_code_text_that_never_claims_the_system_voice():
    assert _TRANSLATE_NOTE.startswith("[") and _TRANSLATE_NOTE.endswith("]")
    assert "system:" not in _TRANSLATE_NOTE.lower() and "[system" not in _TRANSLATE_NOTE.lower()


@pytest.mark.asyncio
async def test_unpressed_interpreter_speech_gets_the_translate_note_before_the_turn():
    call = await _Call("interpreter").start()
    await call.provider_says("speech_started", item_id="item-u1")
    assert call.notes() == [_WEB_PICKUP_NOTE, _TRANSLATE_NOTE]  # in on speech_started, before the commit
    await call.provider_says("speech_stopped", item_id="item-u1")
    await call.provider_says("turn_committed", item_id="item-u1")
    await call.end()
    assert call.provider.conversation == [("system", _WEB_PICKUP_NOTE), ("system", _TRANSLATE_NOTE),
                                          ("user", "item-u1")]
    assert call.ask_states() == []  # the page hears nothing about translation


@pytest.mark.asyncio
async def test_every_utterance_from_either_side_gets_its_own_note():
    call = await _Call("interpreter").start()
    await _utterance(call, "item-u1")
    await _utterance(call, "item-u2")
    await call.end()
    assert call.provider.conversation == [
        ("system", _WEB_PICKUP_NOTE),
        ("system", _TRANSLATE_NOTE), ("user", "item-u1"),
        ("system", _TRANSLATE_NOTE), ("user", "item-u2")]


@pytest.mark.asyncio
async def test_a_pressed_button_gets_only_the_ask_note():
    call = await _Call("interpreter").start()
    await call.page_says({"type": "ask_arm", "seq": 1})
    await _utterance(call, "item-u1")
    await call.end()
    assert call.provider.conversation == [("system", _WEB_PICKUP_NOTE), ("system", _ASK_NOTE), ("user", "item-u1")]


@pytest.mark.asyncio
async def test_a_second_speech_started_for_the_same_utterance_adds_no_note():
    call = await _Call("interpreter").start()
    await call.provider_says("speech_started", item_id="item-u1")
    await call.provider_says("speech_started", item_id="item-u1")
    await call.provider_says("speech_stopped", item_id="item-u1")
    await call.provider_says("turn_committed", item_id="item-u1")
    await call.end()
    assert call.notes() == [_WEB_PICKUP_NOTE, _TRANSLATE_NOTE]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["lelik", "tutor"])
async def test_other_modes_get_no_note(mode):
    call = await _Call(mode).start()
    await _utterance(call, "item-u1")
    await call.end()
    assert call.notes() == [_WEB_PICKUP_NOTE]


@pytest.mark.asyncio
async def test_the_translate_note_does_not_depend_on_the_control_channel():
    call = _Call("interpreter")
    del call.control_plane._config["control_token_sha256"]  # the knob is off: /connect handed out no channel

    async def inbound():
        await call.ends.wait()
        return
        yield  # pragma: no cover

    call._tasks.append(asyncio.ensure_future(call.service.handle_call(
        ticket=TICKET, inbound_audio=inbound(), send_outbound_audio=_noop, clear_outbound_audio=_noop,
        playback=PlaybackTracker())))
    await asyncio.sleep(_SETTLE_S)
    await _utterance(call, "item-u1")
    await call.end()
    assert len(call.hub) == 0
    assert call.provider.conversation == [("system", _WEB_PICKUP_NOTE), ("system", _TRANSLATE_NOTE),
                                          ("user", "item-u1")]
