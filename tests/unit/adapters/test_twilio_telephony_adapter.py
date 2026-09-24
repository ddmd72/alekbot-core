import pytest
from unittest.mock import MagicMock

from src.adapters.twilio_telephony_adapter import TwilioTelephonyAdapter


@pytest.mark.asyncio
async def test_originate_call_uses_answer_url_not_auth_webhook():
    fake_call = MagicMock(sid="CA123")
    fake_client = MagicMock()
    fake_client.calls.create.return_value = fake_call

    adapter = TwilioTelephonyAdapter(account_sid="AC1", auth_token="tok", client_factory=lambda *a, **k: fake_client)

    sid = await adapter.originate_call(
        to="+346001",
        from_="+346002",
        answer_url="https://main.example.com/voice/answer",
        status_callback_url="https://main.example.com/voice/status",
    )

    assert sid == "CA123"
    fake_client.calls.create.assert_called_once_with(
        to="+346001",
        from_="+346002",
        url="https://main.example.com/voice/answer",
        machine_detection="DetectMessageEnd",
        async_amd=True,
        async_amd_status_callback="https://main.example.com/voice/status",
        async_amd_status_callback_method="POST",
        status_callback="https://main.example.com/voice/status",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
    )
    assert "answer" in fake_client.calls.create.call_args.kwargs["url"]
    assert "auth" not in fake_client.calls.create.call_args.kwargs["url"]
