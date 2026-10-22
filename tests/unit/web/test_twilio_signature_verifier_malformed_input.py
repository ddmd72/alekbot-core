"""`verify_twilio_signature` never raises (P7, inventory §4; `src/web/twilio_signature_verifier.py:67-72`): when
Twilio's own validator raises on malformed input, the request is refused, not crashed."""
import pytest

from src.web.twilio_signature_verifier import verify_twilio_signature

_URL = "https://main.example.com/voice/answer?ticket=t1"


@pytest.mark.parametrize("url,form", [
    (_URL, {"CallSid": None}),     # a value that is not a string: RequestValidator.validate raises TypeError
    (_URL, {"CallSid": 7}),
    (None, {"CallSid": "CA1"}),    # no URL at all
])
def test_a_validator_that_raises_is_a_refusal(url, form):
    assert verify_twilio_signature("test-auth-token-0123456789abcdef", url, form, "c2lnbmF0dXJl") is False
