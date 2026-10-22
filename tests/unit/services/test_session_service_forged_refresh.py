"""`SessionService.verify_refresh_token` refuses a token it did not sign (P7, inventory §4;
`src/services/session_service.py:240-242`) and a token of the wrong type."""
import jwt
import pytest

from tests.unit.web.gate_fakes import access_token, forged_token, refresh_token, session_service


def test_forged_refresh_token_raises_invalid_token():
    with pytest.raises(jwt.InvalidTokenError) as raised:
        session_service().verify_refresh_token(forged_token("refresh"))
    assert not isinstance(raised.value, jwt.ExpiredSignatureError)


def test_malformed_refresh_token_raises_invalid_token():
    with pytest.raises(jwt.InvalidTokenError):
        session_service().verify_refresh_token("not.a.jwt")


def test_access_token_is_not_a_refresh_token():
    with pytest.raises(ValueError, match="Expected refresh token"):
        session_service().verify_refresh_token(access_token())


def test_its_own_refresh_token_verifies():
    assert session_service().verify_refresh_token(refresh_token("user-a"))["sub"] == "user-a"
