"""Answers the relay stops waiting for are abandoned, so the main service posts them to chat
(voice UAT round 1, Task 2): on the delegation timeout, and for every delegation still pending
when the call ends - before teardown cancels it and before the transcript is submitted."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.services.voice_session_service as voice_session_service
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

_TOOLS = [{"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}]


def _tool_call(call_id="c1", intent="ask_alek", query="plan my week"):
    return E(type="tool_call", payload={"call_id": call_id, "name": "delegate_to_specialist",
                                        "arguments": json.dumps({"intent": intent, "query": query})})


def _opening():
    return [E(type="response_created", payload={}), E(type="response_done", payload={})]


async def _hold():
    await asyncio.sleep(10)


async def _call(events, delegate, timeout_s=90.0, seconds=0.3, abandon=None):
    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": _TOOLS}
    control.delegate.side_effect = delegate
    if abandon is not None:
        control.abandon_delegation.side_effect = abandon

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


@pytest.mark.asyncio
async def test_delegate_names_the_delegation_for_the_main_side():
    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        await _hold()
        yield  # pragma: no cover

    _, control = await _call(events, AsyncMock(return_value="done"))
    kwargs = control.delegate.await_args.kwargs
    assert kwargs["ticket"] == "t1"
    assert kwargs["call_id"] == "c1"
    assert kwargs["request"] == "ask_alek: plan my week"
    # Answered in time: nothing to abandon.
    control.abandon_delegation.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_abandons_the_delegation_and_promises_chat():
    async def never(**_):
        await asyncio.sleep(10)

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        await _hold()
        yield  # pragma: no cover

    session, control = await _call(events, never, timeout_s=0.05)
    control.abandon_delegation.assert_awaited_once_with("t1", "c1")
    [(call_id, output)] = [c.args for c in session.submit_tool_result.await_args_list]
    assert call_id == "c1"
    assert output == ("The answer is taking long. Tell the caller in one line that it will come to "
                      "their chat.")


@pytest.mark.asyncio
async def test_late_timeout_after_the_caller_spoke_says_the_answer_comes_to_chat():
    async def never(**_):
        await asyncio.sleep(10)

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        yield E(type="speech_started", payload={})
        yield E(type="speech_stopped", payload={})
        await _hold()
        yield  # pragma: no cover

    session, control = await _call(events, never, timeout_s=0.05)
    control.abandon_delegation.assert_awaited_once_with("t1", "c1")
    notes = [c.args[1] for c in session.submit_message.await_args_list if "is taking long" in c.args[1]]
    assert notes == ["[Your earlier request (ask_alek: plan my week) is taking long. Tell the caller "
                     "in one line that the answer will come to their chat.]"]


@pytest.mark.asyncio
async def test_timed_out_delegation_is_not_abandoned_again_when_the_call_ends():
    async def never(**_):
        await asyncio.sleep(10)

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        await _hold()
        yield  # pragma: no cover

    _, control = await _call(events, never, timeout_s=0.05)
    assert control.abandon_delegation.await_count == 1


@pytest.mark.asyncio
async def test_call_end_abandons_every_pending_delegation_before_cancelling_and_submitting():
    order = []

    async def never(**kwargs):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            order.append(f"cancelled:{kwargs['call_id']}")
            raise

    async def abandon(ticket, call_id):
        order.append(f"abandon:{call_id}")

    async def events():
        for e in _opening():
            yield e
        yield _tool_call("c1")
        yield _tool_call("c2", intent="search_web", query="weather")
        await _hold()
        yield  # pragma: no cover

    _, control = await _call(events, never, abandon=abandon)
    assert {c.args for c in control.abandon_delegation.await_args_list} == {("t1", "c1"), ("t1", "c2")}
    assert set(order[:2]) == {"abandon:c1", "abandon:c2"}
    assert set(order[2:]) == {"cancelled:c1", "cancelled:c2"}
    names = [name for name, _, _ in control.mock_calls]
    assert max(i for i, n in enumerate(names) if n == "abandon_delegation") < names.index("submit_transcript")


@pytest.mark.asyncio
async def test_a_failing_abandon_never_breaks_teardown():
    async def never(**_):
        await asyncio.sleep(10)

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        await _hold()
        yield  # pragma: no cover

    _, control = await _call(events, never, abandon=RuntimeError("main service down"))
    control.abandon_delegation.assert_awaited_once_with("t1", "c1")
    control.submit_transcript.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_hanging_abandon_holds_teardown_only_for_its_bound(monkeypatch):
    monkeypatch.setattr(voice_session_service, "_ABANDON_TIMEOUT_S", 0.5)

    async def never(**_):
        await asyncio.sleep(10)

    async def hang(ticket, call_id):
        await asyncio.sleep(10)

    async def events():
        for e in _opening():
            yield e
        yield _tool_call("c1")
        yield _tool_call("c2")
        await _hold()
        yield  # pragma: no cover

    loop = asyncio.get_running_loop()
    started = loop.time()
    _, control = await _call(events, never, abandon=hang)
    # Two pending, abandoned concurrently: one bound (0.5 s) on top of the 0.3 s call, not two (1.0 s).
    assert loop.time() - started < 0.3 + 0.9
    control.submit_transcript.assert_awaited_once()
