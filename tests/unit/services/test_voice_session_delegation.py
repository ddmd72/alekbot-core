import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.services.voice_session_service as voice_module
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

_TOOLS = [{"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}]


def _tool_call(call_id="c1", intent="search_web", query="weather"):
    return E(type="tool_call", payload={"call_id": call_id, "name": "delegate_to_specialist",
                                        "arguments": json.dumps({"intent": intent, "query": query})})


async def _call(events, delegate, seconds=0.3, timeout_s=90.0):
    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": _TOOLS}
    control.delegate.side_effect = delegate

    async def inbound():
        await asyncio.sleep(seconds)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock(),
                                  delegation_timeout_s=timeout_s)
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return session, control


def _opening():
    return [E(type="response_created", payload={}), E(type="response_done", payload={})]


async def _hold():
    await asyncio.sleep(10)


@pytest.mark.asyncio
async def test_session_opens_with_the_configured_tools():
    async def events():
        for e in _opening():
            yield e
    session, _ = await _call(events, AsyncMock())
    assert session.open.await_args.kwargs["tools"] == _TOOLS


@pytest.mark.asyncio
async def test_tool_call_is_forwarded_with_call_context_and_answered_as_function_output():
    async def events():
        for e in _opening():
            yield e
        yield E(type="user_transcript", payload={"text": "what's the weather"})
        yield E(type="response_created", payload={})
        yield _tool_call()
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    session, control = await _call(events, AsyncMock(return_value="sunny"))
    kwargs = control.delegate.await_args.kwargs
    assert kwargs["user_id"] == "u1" and kwargs["account_id"] == "a1"
    assert kwargs["arguments"] == {"intent": "search_web", "query": "weather"}
    assert kwargs["call_context"][-1] == {"role": "user", "text": "what's the weather"}
    session.submit_tool_result.assert_awaited_once_with("c1", "sunny")
    assert session.request_response.await_count == 2  # opening line + the answer


@pytest.mark.asyncio
async def test_answer_after_caller_spoke_arrives_as_fresh_message():
    """Spike 0.1: OpenAI silently drops a late function_call_output after the caller spoke."""
    async def slow(**_):
        await asyncio.sleep(0.05)
        return "sunny"

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        yield E(type="speech_started", payload={})
        yield E(type="speech_stopped", payload={})
        await _hold()
        yield  # pragma: no cover

    session, _ = await _call(events, slow)
    session.submit_tool_result.assert_not_awaited()
    notes = [c.args[1] for c in session.submit_message.await_args_list]
    assert any("just arrived" in n and "sunny" in n for n in notes)


@pytest.mark.asyncio
async def test_answer_waits_while_a_response_is_active():
    release = asyncio.Event()

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        yield E(type="response_created", payload={})  # Lelik is talking when the answer lands
        await release.wait()
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    task = asyncio.ensure_future(_call(events, AsyncMock(return_value="sunny")))
    await asyncio.sleep(0.05)  # the answer is back, the response is still active
    release.set()
    session, _ = await task
    session.submit_tool_result.assert_awaited_once_with("c1", "sunny")
    # opening + one reply after response_done; never a create during the active response
    assert session.request_response.await_count == 2


@pytest.mark.asyncio
async def test_two_answers_queued_behind_active_response_share_one_reply():
    release = asyncio.Event()

    async def events():
        for e in _opening():
            yield e
        yield _tool_call("c1")
        yield _tool_call("c2", intent="search_memory")
        yield E(type="response_created", payload={})
        await release.wait()
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    async def delegate(**kwargs):
        return f"answer:{kwargs['arguments']['intent']}"

    task = asyncio.ensure_future(_call(events, delegate))
    await asyncio.sleep(0.05)
    release.set()
    session, control = await task
    assert control.delegate.await_count == 2
    outputs = {c.args for c in session.submit_tool_result.await_args_list}
    assert outputs == {("c1", "answer:search_web"), ("c2", "answer:search_memory")}
    assert session.request_response.await_count == 2  # opening + ONE reply for both


@pytest.mark.asyncio
async def test_timeout_is_spoken_not_silent():
    async def never(**_):
        await asyncio.sleep(10)

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        await _hold()
        yield  # pragma: no cover

    session, _ = await _call(events, never, timeout_s=0.05)
    [(call_id, output)] = [c.args for c in session.submit_tool_result.await_args_list]
    assert call_id == "c1" and "did not come through" in output


@pytest.mark.asyncio
async def test_transport_failure_is_spoken_and_not_retried():
    failing = AsyncMock(side_effect=RuntimeError("503"))

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        await _hold()
        yield  # pragma: no cover

    session, control = await _call(events, failing)
    control.delegate.assert_awaited_once()
    assert "did not go through" in session.submit_tool_result.await_args.args[1]


@pytest.mark.asyncio
async def test_call_end_cancels_in_flight_delegation_and_discards_answer():
    async def slow(**_):
        await asyncio.sleep(10)
        return "too late"

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        await _hold()
        yield  # pragma: no cover

    session, control = await _call(events, slow, seconds=0.1)
    session.submit_tool_result.assert_not_awaited()
    control.submit_transcript.assert_awaited_once()
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_answer_arriving_while_caller_speaks_defers_the_reply_to_their_turn():
    async def slow(**_):
        await asyncio.sleep(0.05)
        return "sunny"

    async def events():
        for e in _opening():
            yield e
        yield E(type="speech_started", payload={})
        yield _tool_call()  # dispatched after the speech started: not "interrupted"
        await asyncio.sleep(0.1)  # answer lands while still speaking
        yield E(type="speech_stopped", payload={})
        yield E(type="turn_committed", payload={"item_id": "u1"})
        await _hold()
        yield  # pragma: no cover

    session, _ = await _call(events, slow)
    session.submit_tool_result.assert_awaited_once_with("c1", "sunny")
    assert session.request_response.await_count == 2  # opening + the committed turn, no extra


@pytest.mark.asyncio
async def test_watchdog_keeps_the_caller_company_while_a_delegation_is_pending(monkeypatch):
    monkeypatch.setattr(voice_module, "_WATCHDOG_TICK_S", 0.01)

    async def never(**_):
        await asyncio.sleep(10)

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        await _hold()
        yield  # pragma: no cover

    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {"instructions": "x", "user_id": "u1", "account_id": "a1", "tools": _TOOLS}
    control.delegate.side_effect = never

    async def inbound():
        await asyncio.sleep(0.3)
        return
        yield  # pragma: no cover

    await VoiceSessionService(realtime_session_factory=MagicMock(return_value=session), control_plane=control,
                              alert_sink=AsyncMock(), silence_timeout_s=0.05).handle_call(
        ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
        clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    notes = [c.args[1] for c in session.submit_message.await_args_list]
    assert any("Still waiting" in n for n in notes)
    assert not any("has been silent" in n for n in notes)


# =============================================================================
# Fix round 1: response.create is serialized; interruption is judged at injection;
# an error while delivering an answer is logged, never lost.
# =============================================================================

_PERSONA_INSTRUCTIONS = "identity {\n x\n}\nvoice {\n y\n}"


async def _suspend(*_args, **_kwargs):
    # A real websocket send can yield; this double makes every one of them yield.
    await asyncio.sleep(0)


def _suspending_session(events):
    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    session.submit_message.side_effect = _suspend
    session.submit_tool_result.side_effect = _suspend
    return session


async def _call_with(session, delegate, instructions="you are Lelik", seconds=0.3):
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": instructions, "user_id": "u1", "account_id": "a1", "tools": _TOOLS}
    control.delegate.side_effect = delegate

    async def inbound():
        await asyncio.sleep(seconds)
        return
        yield  # pragma: no cover

    await VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                              control_plane=control, alert_sink=AsyncMock()).handle_call(
        ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
        clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return control


@pytest.mark.asyncio
async def test_two_answers_landing_while_idle_start_exactly_one_response():
    """Both answers pass an idle check; only one may send response.create (RFC §4.7: serialize)."""
    async def events():
        for e in _opening():
            yield e
        yield _tool_call("c1")
        yield _tool_call("c2", intent="search_memory")
        await _hold()
        yield  # pragma: no cover

    session = _suspending_session(events)
    await _call_with(session, AsyncMock(return_value="sunny"), instructions=_PERSONA_INSTRUCTIONS)
    assert {c.args[0] for c in session.submit_tool_result.await_args_list} == {"c1", "c2"}
    assert session.request_response.await_count == 2  # opening + ONE for both answers


@pytest.mark.asyncio
async def test_answer_queued_behind_a_response_is_a_fresh_message_if_the_caller_spoke_before_it_went_in():
    """Queued while Lelik talked (not interrupted yet); the caller barges in before the flush."""
    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        yield E(type="response_created", payload={})
        await asyncio.sleep(0.05)  # the answer lands and is queued behind the active response
        yield E(type="speech_started", payload={})
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    session, _ = await _call(events, AsyncMock(return_value="sunny"))
    session.submit_tool_result.assert_not_awaited()
    notes = [c.args[1] for c in session.submit_message.await_args_list]
    assert any("just arrived" in n and "sunny" in n for n in notes)


@pytest.mark.asyncio
async def test_failure_while_delivering_an_answer_is_logged_and_the_call_goes_on(monkeypatch):
    fake_logger = MagicMock()
    monkeypatch.setattr(voice_module, "logger", fake_logger)

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        await _hold()
        yield  # pragma: no cover

    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    session.submit_tool_result.side_effect = RuntimeError("socket closed")
    control = await _call_with(session, AsyncMock(return_value="sunny"))
    logged = [c for c in fake_logger.error.call_args_list if "not delivered" in c.args[0]]
    assert len(logged) == 1 and logged[0].kwargs.get("exc_info") is True
    session.close.assert_awaited_once()
    control.submit_transcript.assert_awaited_once()
