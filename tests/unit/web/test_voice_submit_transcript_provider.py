"""The prompt_content rows of a voice call carry the provider the relay reports; an older relay reports none."""
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from src.web.voice_control_plane_app import create_voice_control_plane_blueprint

_TURN = {"request_text": "hello", "response_text": "hi", "started_at": "2026-11-20T10:00:00+00:00",
         "ended_at": "2026-11-20T10:00:01+00:00", "finish_reason": "completed"}


async def _submit(extra):
    store = AsyncMock()
    store.get.return_value = None
    prompt_content = AsyncMock()
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=store, quota_service=AsyncMock(), prompt_content_store=prompt_content,
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
        digest_key=b"k" * 32))
    response = await app.test_client().post("/voice/submit-transcript", json={
        "call_id": "c1", "user_id": "u1", "account_id": "acc-1", "transcript_text": "hello",
        "usage_by_model": {"m": 10}, "turns": [_TURN, _TURN], **extra}, headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    return prompt_content.record_turn.await_args_list


@pytest.mark.asyncio
async def test_turn_rows_carry_the_reported_provider():
    calls = await _submit({"provider": "xai"})

    assert len(calls) == 2
    assert all(call.kwargs["provider"] == "xai" for call in calls)


@pytest.mark.asyncio
async def test_a_report_without_a_provider_records_unknown():
    calls = await _submit({})

    assert len(calls) == 2
    assert all(call.kwargs["provider"] == "unknown" for call in calls)
