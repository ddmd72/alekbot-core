"""
Unit tests for the /f/<token> file-access blueprint.

Covers: valid non-gated token → 302 to signed URL; expired/invalid → 401;
gated token without cookie → redirect to login; gated with wrong user → 403;
gated with matching Cabinet cookie → 302.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from quart import Quart

from src.services.file_access_token_service import FileAccessTokenService
from src.web.file_access_app import create_file_access_blueprint


_SECRET = "q" * 40
_SIGNED = "https://storage.googleapis.com/bucket/key?X-Goog-Signature=abc"


@pytest.fixture
def tokens():
    return FileAccessTokenService(secret_key=_SECRET)


@pytest.fixture
def media():
    m = MagicMock()
    m.generate_signed_url = AsyncMock(return_value=_SIGNED)
    return m


@pytest.fixture
def session_service():
    return MagicMock()


def _app(tokens, media, session_service):
    app = Quart("test_app")
    app.register_blueprint(
        create_file_access_blueprint(
            token_service=tokens, media_storage=media, session_service=session_service
        )
    )
    return app


class TestNonGated:

    async def test_valid_token_redirects_to_signed_url(self, tokens, media, session_service):
        app = _app(tokens, media, session_service)
        token = tokens.mint(key="docs/x.pdf", user_id="u1")  # not gated
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        assert resp.status_code == 302
        assert resp.headers["Location"] == _SIGNED
        media.generate_signed_url.assert_awaited_once()
        assert media.generate_signed_url.call_args.args[0] == "docs/x.pdf"

    async def test_signed_url_ttl_is_short(self, tokens, media, session_service):
        app = _app(tokens, media, session_service)
        token = tokens.mint(key="docs/x.pdf", user_id="u1")
        async with app.test_client() as client:
            await client.get(f"/f/{token}")
        # 5-minute signed URL ttl
        assert media.generate_signed_url.call_args.args[1] == 300

    async def test_no_cookie_needed_for_non_gated(self, tokens, media, session_service):
        app = _app(tokens, media, session_service)
        token = tokens.mint(key="deep_research/u1/ts.md", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        assert resp.status_code == 302
        session_service.verify_access_token.assert_not_called()


class TestInvalidTokens:

    async def test_expired_token_401(self, tokens, media, session_service):
        app = _app(tokens, media, session_service)
        token = tokens.mint(key="docs/x.pdf", user_id="u1", ttl_seconds=-1)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        assert resp.status_code == 401
        media.generate_signed_url.assert_not_called()

    async def test_garbage_token_401(self, tokens, media, session_service):
        app = _app(tokens, media, session_service)
        async with app.test_client() as client:
            resp = await client.get("/f/not-a-jwt")
        assert resp.status_code == 401


class TestGated:

    async def test_gated_without_cookie_redirects_to_login(self, tokens, media, session_service):
        app = _app(tokens, media, session_service)
        token = tokens.mint(key="email_review/r.html", user_id="u1", gated=True)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]
        media.generate_signed_url.assert_not_called()

    async def test_gated_wrong_user_403(self, tokens, media, session_service):
        session_service.verify_access_token = MagicMock(return_value={"sub": "OTHER", "account_id": "a"})
        app = _app(tokens, media, session_service)
        token = tokens.mint(key="email_review/r.html", user_id="u1", gated=True)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}", headers={"Cookie": "access_token=valid"})
        assert resp.status_code == 403
        media.generate_signed_url.assert_not_called()

    async def test_gated_matching_user_302(self, tokens, media, session_service):
        session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a"})
        app = _app(tokens, media, session_service)
        token = tokens.mint(key="email_review/r.html", user_id="u1", gated=True)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}", headers={"Cookie": "access_token=valid"})
        assert resp.status_code == 302
        assert resp.headers["Location"] == _SIGNED

    async def test_gated_invalid_cookie_redirects_to_login(self, tokens, media, session_service):
        session_service.verify_access_token = MagicMock(side_effect=Exception("bad"))
        app = _app(tokens, media, session_service)
        token = tokens.mint(key="email_review/r.html", user_id="u1", gated=True)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}", headers={"Cookie": "access_token=bad"})
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]


class TestSigningFailure:

    async def test_signing_error_returns_503(self, tokens, media, session_service):
        media.generate_signed_url = AsyncMock(side_effect=RuntimeError("iam down"))
        app = _app(tokens, media, session_service)
        token = tokens.mint(key="docs/x.pdf", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        assert resp.status_code == 503
