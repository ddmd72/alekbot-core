"""/voice/submit-transcript waits for the prompt-content writes it scheduled before it
answers. Cloud Run throttles the CPU once the response is sent; writes left pending after
that starved until the next request and then failed with SSL EOF (2026-09-23)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.web.voice_control_plane_app import create_voice_control_plane_blueprint

_PAYLOAD = {
    "call_id": "c1", "user_id": "u1", "account_id": "a1", "transcript_text": "hi",
    "usage_by_model": {"gpt-realtime-2.1": {"audio_input_tokens": 1}},
    "turns": [{"request_text": "hi", "response_text": "hello", "finish_reason": "completed",
               "started_at": "2026-09-23T10:00:00+00:00", "ended_at": "2026-09-23T10:00:02+00:00"}],
}


def _client(order, summary_consumer=None):
    store = MagicMock()
    store.record_turn = AsyncMock(side_effect=lambda **_: order.append("record_turn"))
    store.flush = AsyncMock(side_effect=lambda **_: order.append("flush"))

    async def consumer(**_):
        order.append("summary")

    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=AsyncMock(), quota_service=AsyncMock(), prompt_content_store=store,
        summary_consumer=summary_consumer or consumer, oidc_verifier=AsyncMock(return_value=True),
        alert_sink=AsyncMock(),
    ))
    return app.test_client(), store


@pytest.mark.asyncio
async def test_pending_writes_are_flushed_after_everything_that_schedules_them():
    order = []
    client, store = _client(order)
    response = await client.post("/voice/submit-transcript", json=_PAYLOAD, headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    # The summarizer's own LLM turn is captured too, so flush comes last.
    assert order == ["summary", "record_turn", "flush"]
    store.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_flush_still_runs_when_the_summary_pipeline_fails():
    order = []
    client, store = _client(order, summary_consumer=AsyncMock(side_effect=RuntimeError("boom")))
    response = await client.post("/voice/submit-transcript", json=_PAYLOAD, headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    store.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failing_flush_does_not_fail_the_request():
    order = []
    client, store = _client(order)
    store.flush.side_effect = RuntimeError("bq")
    response = await client.post("/voice/submit-transcript", json=_PAYLOAD, headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
