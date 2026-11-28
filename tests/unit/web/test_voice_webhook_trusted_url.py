"""The signature URL comes from the service's known public base; and an unbound caller alerts once per day."""
import logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.web.voice_webhook_app import create_voice_webhook_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.voice_callback import callback_ref_for

_CIPHER = AesGcmTestCipher()


def _app(*, verifier=None, public_base_url="", alert_sink=None, ephemeral_store=None, user_repository=None):
    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=user_repository or AsyncMock(),
        ephemeral_store=ephemeral_store or AsyncMock(),
        alert_sink=alert_sink or AsyncMock(),
        notification_service=AsyncMock(),
        lelik_agent_provider=AsyncMock(),
        answer_url="https://main.example.com/voice/answer",
        signature_verifier=verifier or AsyncMock(return_value=True),
        relay_stream_url="wss://relay.example.com/", cipher=_CIPHER, digest_key=b"k" * 32,
        public_base_url=public_base_url,
    ))
    return app


@pytest.mark.asyncio
async def test_the_signature_url_comes_from_the_public_base_whatever_the_forwarded_headers():
    verifier = AsyncMock(return_value=False)
    ref = await callback_ref_for(_CIPHER, "t1")
    client = _app(verifier=verifier, public_base_url="https://main.example.com").test_client()

    response = await client.post(
        f"/voice/answer?ref={ref}", form={"CallSid": "CA1"},
        headers={"X-Twilio-Signature": "sig", "X-Forwarded-Proto": "http", "X-Forwarded-Host": "other.example.org"},
    )

    assert response.status_code == 403
    assert verifier.await_args.args[0] == f"https://main.example.com/voice/answer?ref={ref}"


@pytest.mark.asyncio
async def test_empty_base_on_cloud_run_never_builds_a_url(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "main")
    verifier = AsyncMock(return_value=False)
    client = _app(verifier=verifier).test_client()

    await client.post("/voice/auth", form={"From": "+15550101"},
                      headers={"X-Forwarded-Host": "other.example.org"})

    assert verifier.await_args.args[0] == ""


@pytest.mark.asyncio
async def test_unbound_caller_alerts_once_per_number_and_never_logs_the_number(caplog):
    user_repository = AsyncMock()
    user_repository.get_user_by_platform_id.return_value = None
    alert_sink = AsyncMock()
    ephemeral_store = AsyncMock()
    ephemeral_store.set_if_absent = AsyncMock(side_effect=[True, False, False])
    client = _app(alert_sink=alert_sink, ephemeral_store=ephemeral_store,
                  user_repository=user_repository).test_client()

    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            response = await client.post("/voice/auth", form={"From": "+15550101"})
            assert "<Reject" in (await response.get_data()).decode()

    alert_sink.post.assert_awaited_once()
    assert "+15550101" not in alert_sink.post.await_args.args[0]
    assert "+15550101" not in caplog.text
    keys = {call.args[0] for call in ephemeral_store.set_if_absent.await_args_list}
    assert len(keys) == 1 and "+15550101" not in next(iter(keys))
    assert ephemeral_store.set_if_absent.await_args_list[0].kwargs["ttl_s"] == 86400
