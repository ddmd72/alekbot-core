"""Twilio signature verification (final whole-branch review, FIX I3).

Signatures here are computed with `RequestValidator.compute_signature` — the
same code path Twilio's own servers use — rather than hand-rolled or faked. A
test built on a made-up signature string cannot distinguish "the verifier
rejects everything" from "the verifier works", which is the failure mode this
file exists to rule out.
"""
import pytest
from twilio.request_validator import RequestValidator

from src.web.twilio_signature_verifier import verify_twilio_signature

_AUTH_TOKEN = "test-auth-token-0123456789abcdef"
_URL = "https://main.example.com/voice/answer?ticket=t1"
_FORM = {"CallSid": "CA1", "AnsweredBy": "human", "From": "+346001"}


def _sign(url: str, form: dict, token: str = _AUTH_TOKEN) -> str:
    return RequestValidator(token).compute_signature(url, form)


def test_valid_signature_accepted():
    assert verify_twilio_signature(_AUTH_TOKEN, _URL, _FORM, _sign(_URL, _FORM)) is True


def test_tampered_form_parameter_rejected():
    """Twilio's HMAC covers every POST parameter, sorted by name."""
    signature = _sign(_URL, _FORM)
    tampered = {**_FORM, "From": "+346002"}
    assert verify_twilio_signature(_AUTH_TOKEN, _URL, tampered, signature) is False


def test_swapped_ticket_in_the_query_string_rejected():
    """The load-bearing case for the voice routes: the call ticket lives in the
    query string, and the signature covers the FULL url — so an attacker who
    captures a signed callback cannot point it at a different ticket.
    """
    signature = _sign(_URL, _FORM)
    other_ticket_url = "https://main.example.com/voice/answer?ticket=someone-elses"
    assert verify_twilio_signature(_AUTH_TOKEN, other_ticket_url, _FORM, signature) is False


def test_signature_from_a_different_auth_token_rejected():
    foreign = _sign(_URL, _FORM, token="a-different-account-token")
    assert verify_twilio_signature(_AUTH_TOKEN, _URL, _FORM, foreign) is False


@pytest.mark.parametrize("signature", ["", None, "not-base64-at-all"])
def test_missing_or_garbage_signature_rejected_without_raising(signature):
    assert verify_twilio_signature(_AUTH_TOKEN, _URL, _FORM, signature) is False


def test_no_auth_token_configured_rejects_rather_than_passing():
    """Fail closed. The local-dev bypass (no TWILIO_AUTH_TOKEN -> skip the
    check entirely) lives in main.py's wiring, deliberately NOT in here — this
    function's only job is the cryptographic answer, and 'no key' is not a
    valid signature.
    """
    assert verify_twilio_signature("", _URL, _FORM, _sign(_URL, _FORM)) is False


def test_get_style_request_with_no_form_parameters():
    """Twilio signs status callbacks with an empty body the same way."""
    url = "https://main.example.com/voice/status?ticket=t1"
    assert verify_twilio_signature(_AUTH_TOKEN, url, {}, _sign(url, {})) is True
