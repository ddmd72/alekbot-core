"""Async AMD: Twilio posts the machine-detection verdict to /voice/status (no CallStatus),
and the call is already connected — the verdict is only recorded, never acted on here."""
import pytest
from unittest.mock import AsyncMock

from quart import Quart

from src.web.voice_webhook_app import create_voice_webhook_blueprint


def _app(ephemeral_store):
    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=AsyncMock(),
        ephemeral_store=ephemeral_store,
        alert_sink=AsyncMock(),
        notification_service=AsyncMock(),
        lelik_agent_provider=AsyncMock(),
        answer_url="https://main.example.com/voice/answer",
        signature_verifier=AsyncMock(return_value=True),
    ))
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("answered_by", ["machine_end_beep", "human", "unknown"])
async def test_amd_verdict_is_recorded_for_the_ticket_and_releases_nothing(answered_by):
    ephemeral_store = AsyncMock()

    client = _app(ephemeral_store).test_client()
    response = await client.post(
        "/voice/status?ticket=t1",
        form={"CallSid": "CA1", "AccountSid": "AC1", "AnsweredBy": answered_by, "MachineDetectionDuration": "4200"},
    )

    assert response.status_code == 200
    ephemeral_store.set.assert_awaited_once()
    key, value = ephemeral_store.set.await_args.args[:2]
    assert key == "voice_amd:t1"
    assert value == {"answered_by": answered_by}
    # The call is live: the ticket and the one-call marker belong to it until it ends.
    ephemeral_store.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_amd_verdict_is_rejected_when_unsigned():
    ephemeral_store = AsyncMock()
    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=AsyncMock(), ephemeral_store=ephemeral_store, alert_sink=AsyncMock(),
        notification_service=AsyncMock(), lelik_agent_provider=AsyncMock(),
        answer_url="https://main.example.com/voice/answer",
        signature_verifier=AsyncMock(return_value=False),
    ))

    response = await app.test_client().post(
        "/voice/status?ticket=t1", form={"CallSid": "CA1", "AnsweredBy": "human"},
    )

    assert response.status_code == 403
    ephemeral_store.set.assert_not_awaited()
