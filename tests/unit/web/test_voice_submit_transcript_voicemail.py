"""A call the async AMD judged a machine, with no real back-and-forth, is voicemail:
its summary never reaches long-term memory (RFC §4.6). Everything else is a live call."""
import pytest
from unittest.mock import AsyncMock

from quart import Quart

from src.web.voice_control_plane_app import create_voice_control_plane_blueprint


def _turn(request_text):
    return {"request_text": request_text, "response_text": "lelik", "started_at": "2026-09-25T10:00:00+00:00",
            "ended_at": "2026-09-25T10:00:02+00:00", "finish_reason": "completed"}


def _app(verdict):
    ephemeral_store = AsyncMock()

    async def get(key):
        return verdict if key == "voice_amd:t1" else None

    ephemeral_store.get.side_effect = get
    summary_consumer = AsyncMock()
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=ephemeral_store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=summary_consumer, oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
    ))
    return app, ephemeral_store, summary_consumer


async def _submit(app, caller_texts):
    return await app.test_client().post("/voice/submit-transcript", json={
        "call_id": "t1", "user_id": "u1", "account_id": "a1", "transcript_text": "…",
        "usage_by_model": {}, "turns": [_turn(text) for text in caller_texts],
    })


@pytest.mark.asyncio
async def test_machine_verdict_with_a_monologue_skips_the_summary_but_still_releases():
    app, ephemeral_store, summary_consumer = _app({"answered_by": "machine_end_beep"})

    # A voicemail greeting can split into two turns while Lelik talks over it.
    response = await _submit(app, ["", "Hi, you've reached Dima,", "leave a message after the tone"])

    assert response.status_code == 200
    summary_consumer.assert_not_awaited()
    ephemeral_store.delete.assert_awaited_once_with("voice_one_call:u1")


@pytest.mark.asyncio
async def test_machine_verdict_but_a_real_conversation_keeps_the_summary():
    # The owner starts with a long request, so AMD calls it a machine — then keeps talking.
    app, _, summary_consumer = _app({"answered_by": "machine_end_other"})

    await _submit(app, ["what's the weather in Valencia tomorrow", "and in the evening?", "thanks, bye"])

    summary_consumer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [{"answered_by": "human"}, {"answered_by": "unknown"}, None])
async def test_human_unknown_or_missing_verdict_keeps_the_summary(verdict):
    app, _, summary_consumer = _app(verdict)

    await _submit(app, ["hello"])

    summary_consumer.assert_awaited_once()
