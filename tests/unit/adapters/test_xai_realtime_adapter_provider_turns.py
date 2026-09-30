"""XaiRealtimeAdapter implements PROVIDER turn ownership only (VOICE_MULTI_PROVIDER_RFC §4.3/§4.4)."""
import json
from unittest.mock import AsyncMock

import pytest

from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.domain.voice_turn_ownership import TurnOwnership


class _FakeWebSocket:
    def __init__(self, incoming):
        self.sent, self._incoming = [], incoming

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return json.dumps(self._incoming.pop(0))

    async def close(self):
        pass


async def _opened(incoming=()):
    ws = _FakeWebSocket(list(incoming))
    adapter = XaiRealtimeAdapter(api_key="k", voice="castor", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="high", tools=[])
    return adapter, ws


class TestXaiProviderTurns:
    def test_declares_provider_ownership_only(self):
        assert XaiRealtimeAdapter.supported_turn_ownership == frozenset({TurnOwnership.PROVIDER})

    @pytest.mark.asyncio
    async def test_session_lets_xai_reply_and_interrupt_on_its_own(self):
        _, ws = await _opened()
        session = ws.sent[0]["session"]
        assert session["voice"] == "castor"
        assert session["turn_detection"] == {
            "type": "server_vad", "silence_duration_ms": 800, "create_response": True, "interrupt_response": True,
        }

    @pytest.mark.asyncio
    async def test_its_own_replies_pass_through_and_nothing_is_cancelled(self):
        adapter, ws = await _opened([
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.output_audio.delta", "response_id": "r1", "item_id": "i", "delta": "x"},
            {"type": "response.done", "response_id": "r1", "usage": {"billable_audio_seconds": 3}},
        ])
        ws.sent.clear()

        events = [e async for e in adapter.receive_events()]

        assert [e.type for e in events] == ["response_created", "audio_delta", "response_done"]
        assert events[2].payload["usage"]["billable_audio_seconds"] == 3
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_request_response_is_a_plain_response_create(self):
        adapter, ws = await _opened()
        ws.sent.clear()
        await adapter.request_response()
        assert ws.sent == [{"type": "response.create"}]
