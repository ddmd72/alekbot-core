"""xAI session resumption on the wire (VOICE_CALL_CONTROL_CHANNEL_RFC §6, live probe P3, 2026-11-08): the URL carries
`conversation_id`, `resumption.enabled` rides every session.update, `conversation.created` becomes a `resume_handle`
event, and a resume is honoured exactly when items arrive with `replayed: true` before `session.updated`."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter

_CREATED = {"type": "conversation.created", "conversation": {"id": "conv-0001-invented"}}
_UPDATED = {"type": "session.updated", "session": {}}
_REPLAYED = {"type": "conversation.item.added", "replayed": True,
             "item": {"id": "i1", "role": "assistant", "content": [{"type": "text", "text": "Got it, lantern."}]}}
_LIVE_ITEM = {"type": "conversation.item.added", "replayed": False, "item": {"id": "i9", "role": "user"}}


class _Socket:
    """Serves scripted events through recv() and async iteration alike; blocks (like a quiet socket) when empty."""

    def __init__(self, incoming):
        self.sent, self._incoming = [], list(incoming)

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self._incoming:
            await asyncio.sleep(3600)
        return json.dumps(self._incoming.pop(0))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return json.dumps(self._incoming.pop(0))

    async def close(self):
        pass


async def _open(incoming, resume_handle=None):
    ws = _Socket(incoming)
    connect = AsyncMock(return_value=ws)
    adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=connect)
    await adapter.open(instructions="hi", reasoning_effort="high", tools=[], resume_handle=resume_handle)
    return adapter, ws, connect


async def _events(adapter):
    return [event async for event in adapter.receive_events()]


@pytest.mark.asyncio
async def test_fresh_open_has_no_conversation_id_but_opts_in_to_resumption():
    adapter, ws, connect = await _open([])

    assert connect.await_args.args[0] == "wss://api.x.ai/v1/realtime?model=grok-voice-3.0"
    assert ws.sent[0]["session"]["resumption"] == {"enabled": True}
    assert adapter.resumed is False


@pytest.mark.asyncio
async def test_resume_open_adds_the_conversation_id_to_the_url_and_still_opts_in():
    _, ws, connect = await _open([_CREATED, _REPLAYED, _UPDATED], resume_handle="conv-0001-invented")

    assert connect.await_args.args[0] == "wss://api.x.ai/v1/realtime?model=grok-voice-3.0&conversation_id=conv-0001-invented"
    assert ws.sent[0]["session"]["resumption"] == {"enabled": True}


@pytest.mark.asyncio
async def test_conversation_created_is_a_resume_handle_event():
    adapter, _, _ = await _open([_CREATED, {"type": "ping"}])

    events = await _events(adapter)

    assert [(e.type, e.payload) for e in events] == [("resume_handle", {"handle": "conv-0001-invented"})]


@pytest.mark.asyncio
async def test_replay_before_session_updated_means_resumed_and_the_handle_still_arrives():
    adapter, _, _ = await _open([_CREATED, _REPLAYED, _REPLAYED, _UPDATED,
                                 {"type": "response.created", "response": {"id": "r1"}}],
                                resume_handle="conv-0001-invented")

    events = await _events(adapter)

    assert adapter.resumed is True
    # The replayed history is not a new turn: it reaches the service as no event at all.
    assert [e.type for e in events] == ["resume_handle", "response_created"]
    assert events[0].payload == {"handle": "conv-0001-invented"}


@pytest.mark.asyncio
async def test_no_replay_before_session_updated_means_not_honoured_and_the_call_goes_on_fresh():
    # A bogus or expired id raises no error: the server opens a fresh conversation and echoes the id.
    adapter, _, _ = await _open([_CREATED, _UPDATED, {"type": "response.created", "response": {"id": "r1"}}],
                                resume_handle="conv-bogus-invented")

    events = await _events(adapter)

    assert adapter.resumed is False
    assert [e.type for e in events] == ["resume_handle", "response_created"]


@pytest.mark.asyncio
async def test_a_live_item_is_not_a_replay():
    adapter, _, _ = await _open([_CREATED, _LIVE_ITEM, _UPDATED], resume_handle="conv-0001-invented")

    assert adapter.resumed is False


@pytest.mark.asyncio
async def test_a_silent_socket_gives_up_waiting_and_opens_fresh(monkeypatch):
    monkeypatch.setattr("src.adapters.xai_realtime_adapter._RESUME_WAIT_S", 0.05)

    adapter, _, _ = await _open([], resume_handle="conv-0001-invented")

    assert adapter.resumed is False
