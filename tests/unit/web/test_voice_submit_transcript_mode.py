"""An interpreter or tutor call ends without a summary; usage is recorded either way."""
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from tests.support.voice_tickets import DIGEST_KEY, call_kind_key


def _app(record):
    ephemeral_store = AsyncMock()

    async def get(key):
        return record if key == call_kind_key("t1") else None

    ephemeral_store.get.side_effect = get
    summary_consumer, quota = AsyncMock(), AsyncMock()
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=ephemeral_store, quota_service=quota, prompt_content_store=AsyncMock(),
        summary_consumer=summary_consumer, oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
        digest_key=DIGEST_KEY))
    return app, summary_consumer, quota


async def _submit(app):
    return await app.test_client().post("/voice/submit-transcript", json={
        "call_id": "t1", "user_id": "u1", "account_id": "a1", "transcript_text": "hello",
        "usage_by_model": {"m": 10}, "turns": [],
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interpreter", "tutor"])
async def test_mode_calls_skip_the_summary_but_record_usage(mode):
    app, summary_consumer, quota = _app({"call_kind": "web", "mode": mode})

    response = await _submit(app)

    assert response.status_code == 200
    summary_consumer.assert_not_awaited()
    quota.record_usage.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [{"call_kind": "web", "mode": "lelik"}, {"call_kind": "phone"}, None,
                                    {"call_kind": "web", "mode": "something-new"}])
async def test_lelik_missing_or_unknown_mode_keeps_the_summary(record):
    app, summary_consumer, _ = _app(record)

    await _submit(app)

    summary_consumer.assert_awaited_once()
