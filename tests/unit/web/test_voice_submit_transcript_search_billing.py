"""xAI's built-in search calls reach the end-of-call bill: XaiRealtimeAdapter's response_done usage -> VoiceCallBuffer
-> the submit-transcript body's usage_by_model -> /voice/submit-transcript prices it with calculate_realtime_cost."""
import json
import logging
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.domain.billing import calculate_realtime_cost
from src.domain.voice_call_buffer import VoiceCallBuffer
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint


def _app(quota_service):
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = None  # no AMD verdict, no call-mode record: a live companion call
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=ephemeral_store, quota_service=quota_service, prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
        digest_key=b"k" * 32,
    ))
    return app


async def _submit(usage_by_model: dict) -> AsyncMock:
    quota_service = AsyncMock()
    response = await _app(quota_service).test_client().post(
        "/voice/submit-transcript", headers={"Authorization": "Bearer x"},
        json={"call_id": "c1", "user_id": "u1", "account_id": "a1", "transcript_text": "hi", "turns": [],
              "usage_by_model": usage_by_model})
    assert response.status_code == 200
    return quota_service


class _Socket:
    def __init__(self, incoming):
        self._incoming = incoming

    async def send(self, raw):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return json.dumps(self._incoming.pop(0))


def _done(seconds_total: int, searches: list) -> dict:
    output = [{"type": "message", "role": "assistant"}] + [
        {"type": "function_call", "name": name, "call_id": f"c{i}", "arguments": "{}"} for i, name in enumerate(searches)]
    return {"type": "response.done", "usage": {"billable_audio_seconds": seconds_total,
                                               "input_token_details": {"text_tokens": 0}},
            "response": {"status": "completed", "output": output}}


@pytest.mark.asyncio
async def test_search_calls_counted_by_the_adapter_are_billed_at_submit_transcript():
    socket = _Socket([_done(30, ["web_search", "web_search"]), _done(60, ["x_search"]), _done(90, [])])
    adapter = XaiRealtimeAdapter(api_key="k", ws_connect=AsyncMock(return_value=socket), builtin_search=True)
    await adapter.open(instructions="hi", reasoning_effort="high", tools=[])
    buffer = VoiceCallBuffer(call_id="c1")
    async for event in adapter.receive_events():
        buffer.add_usage(event.payload["model"], **event.payload["usage"])

    quota_service = await _submit(buffer.usage_by_model)

    account_id, model, tokens, cost = quota_service.record_usage.await_args.args
    assert (account_id, model, tokens) == ("a1", "grok-voice-3.0", 0)
    # 90 s of audio at $0.08/min + 2 web searches at $5/1k + 1 x_search priced as 10 items at $10/1k.
    assert cost == round(0.12 + 0.01 + 0.1, 6)
    assert cost == calculate_realtime_cost("grok-voice-3.0", buffer.usage_by_model["grok-voice-3.0"])


@pytest.mark.asyncio
async def test_an_unpriced_search_leg_is_logged_as_an_error_not_billed_silently(caplog):
    with caplog.at_level(logging.ERROR):
        quota_service = await _submit({"grok-voice-think-fast-2.0": {"billable_audio_seconds": 60, "web_search_calls": 3}})

    *_, cost = quota_service.record_usage.await_args.args
    assert cost == 0.08  # the seconds only: the deprecated model has no search price
    assert any(r.levelno == logging.ERROR and "web_search_calls" in r.getMessage() and "$0" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_a_priced_search_leg_logs_no_error(caplog):
    with caplog.at_level(logging.ERROR):
        await _submit({"grok-voice-3.0": {"billable_audio_seconds": 60, "web_search_calls": 3}})

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
