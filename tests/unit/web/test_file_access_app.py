"""
Unit tests for the /f/<token> file-access blueprint.

Covers: valid non-gated token → 302 to signed URL; expired/invalid → 401;
gated token without cookie → redirect to login; gated with wrong user → 403;
gated with matching Cabinet cookie → 302.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from quart import Quart

from src.services.file_access_token_service import FileAccessTokenService
from src.web.cabinet_auth import ACCESS_COOKIE
from src.web.file_access_app import create_file_access_blueprint
from tests.support.cabinet_session_kit import CabinetSessionKit


_SECRET = "q" * 40
_SIGNED = "https://storage.googleapis.com/bucket/key?X-Goog-Signature=abc"


@pytest.fixture
def tokens():
    return FileAccessTokenService(secret_key=_SECRET)


@pytest.fixture
def media():
    m = MagicMock()
    m.exists = AsyncMock(return_value=True)
    m.generate_signed_url = AsyncMock(return_value=_SIGNED)
    return m


@pytest.fixture
def kit():
    """The Cabinet device-session kit the blueprint's verifier belongs to."""
    return CabinetSessionKit()


def _app(tokens, media, kit):
    app = Quart("test_app")
    app.register_blueprint(
        create_file_access_blueprint(
            token_service=tokens, media_storage=media, cabinet_sessions=kit.verifier
        )
    )
    return app


class TestNonGated:

    async def test_valid_token_redirects_to_signed_url(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="docs/x.pdf", user_id="u1")  # not gated
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        assert resp.status_code == 302
        assert resp.headers["Location"] == _SIGNED
        media.generate_signed_url.assert_awaited_once()
        assert media.generate_signed_url.call_args.args[0] == "docs/x.pdf"

    async def test_signed_url_ttl_is_short(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="docs/x.pdf", user_id="u1")
        async with app.test_client() as client:
            await client.get(f"/f/{token}")
        # 5-minute signed URL ttl
        assert media.generate_signed_url.call_args.args[1] == 300

    async def test_no_cookie_needed_for_non_gated(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="deep_research/u1/ts.md", user_id="u1")
        with patch.object(kit.verifier, "identify", wraps=kit.verifier.identify) as identify:
            async with app.test_client() as client:
                resp = await client.get(f"/f/{token}")
        assert resp.status_code == 302
        identify.assert_not_called()


class TestInvalidTokens:

    async def test_expired_token_401(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="docs/x.pdf", user_id="u1", ttl_seconds=-1)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        assert resp.status_code == 401
        media.generate_signed_url.assert_not_called()

    async def test_garbage_token_401(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        async with app.test_client() as client:
            resp = await client.get("/f/not-a-jwt")
        assert resp.status_code == 401


class TestGated:

    async def test_gated_without_cookie_redirects_to_login(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="email_review/r.html", user_id="u1", gated=True)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]
        media.generate_signed_url.assert_not_called()

    async def test_gated_wrong_user_403(self, tokens, media, kit):
        other = kit.sign_in_now("OTHER", "a")
        app = _app(tokens, media, kit)
        token = tokens.mint(key="email_review/r.html", user_id="u1", gated=True)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}", headers={"Cookie": f"{ACCESS_COOKIE}={other.access_token}"})
        assert resp.status_code == 403
        media.generate_signed_url.assert_not_called()

    async def test_gated_matching_user_302(self, tokens, media, kit):
        owner = kit.sign_in_now("u1", "a")
        app = _app(tokens, media, kit)
        token = tokens.mint(key="email_review/r.html", user_id="u1", gated=True)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}", headers={"Cookie": f"{ACCESS_COOKIE}={owner.access_token}"})
        assert resp.status_code == 302
        assert resp.headers["Location"] == _SIGNED

    async def test_gated_invalid_cookie_redirects_to_login(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="email_review/r.html", user_id="u1", gated=True)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}", headers={"Cookie": f"{ACCESS_COOKIE}=bad"})
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]


class TestSigningFailure:

    async def test_signing_error_returns_503(self, tokens, media, kit):
        media.generate_signed_url = AsyncMock(side_effect=RuntimeError("iam down"))
        app = _app(tokens, media, kit)
        token = tokens.mint(key="docs/x.pdf", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        assert resp.status_code == 503


class TestVideoLandingPage:
    """video_generation/ keys render a Download+Share page instead of redirecting."""

    async def test_video_key_returns_html_page_not_redirect(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="video_generation/u1/ts.mp4", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/html")
        media.generate_signed_url.assert_not_called()

    async def test_video_page_has_download_and_share(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="video_generation/u1/ts.mp4", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        html = (await resp.get_data()).decode()
        assert "Download" in html
        assert "Share" in html
        assert f"/f/{token}/raw" in html

    async def test_video_page_has_video_preview(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="video_generation/u1/ts.mp4", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        html = (await resp.get_data()).decode()
        assert f'<video controls playsinline src="/f/{token}/raw">' in html

    async def test_non_video_key_still_redirects(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="deep_research/u1/report.html", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}")
        assert resp.status_code == 302


class TestRawRoute:
    """/f/<token>/raw — same-origin byte stream backing the video page's buttons."""

    @pytest.fixture
    def media(self):
        m = MagicMock()
        m.fetch = AsyncMock(return_value=b"fake-mp4-bytes")
        return m

    async def test_returns_bytes_with_attachment_disposition(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="video_generation/u1/ts.mp4", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}/raw")
        assert resp.status_code == 200
        assert await resp.get_data() == b"fake-mp4-bytes"
        assert "attachment" in resp.headers["Content-Disposition"]
        assert "ts.mp4" in resp.headers["Content-Disposition"]
        media.fetch.assert_awaited_once_with("video_generation/u1/ts.mp4")

    async def test_expired_token_401(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="video_generation/u1/ts.mp4", user_id="u1", ttl_seconds=-1)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}/raw")
        assert resp.status_code == 401
        media.fetch.assert_not_called()

    async def test_fetch_failure_returns_503(self, tokens, media, kit):
        media.fetch = AsyncMock(side_effect=RuntimeError("gcs down"))
        app = _app(tokens, media, kit)
        token = tokens.mint(key="video_generation/u1/ts.mp4", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}/raw")
        assert resp.status_code == 503

    async def test_gated_without_cookie_redirects_to_login(self, tokens, media, kit):
        app = _app(tokens, media, kit)
        token = tokens.mint(key="email_review/r.html", user_id="u1", gated=True)
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}/raw")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]
        media.fetch.assert_not_called()

    async def test_range_request_returns_206_partial_content(self, tokens, media, kit):
        media.fetch = AsyncMock(return_value=b"0123456789")
        app = _app(tokens, media, kit)
        token = tokens.mint(key="video_generation/u1/ts.mp4", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}/raw", headers={"Range": "bytes=2-5"})
        assert resp.status_code == 206
        assert await resp.get_data() == b"2345"
        assert resp.headers["Content-Range"] == "bytes 2-5/10"
        assert resp.headers["Content-Length"] == "4"
        assert resp.headers["Accept-Ranges"] == "bytes"

    async def test_range_request_open_ended_returns_rest_of_file(
        self, tokens, media, kit
    ):
        media.fetch = AsyncMock(return_value=b"0123456789")
        app = _app(tokens, media, kit)
        token = tokens.mint(key="video_generation/u1/ts.mp4", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}/raw", headers={"Range": "bytes=7-"})
        assert resp.status_code == 206
        assert await resp.get_data() == b"789"
        assert resp.headers["Content-Range"] == "bytes 7-9/10"

    async def test_no_range_header_returns_full_200(self, tokens, media, kit):
        media.fetch = AsyncMock(return_value=b"0123456789")
        app = _app(tokens, media, kit)
        token = tokens.mint(key="video_generation/u1/ts.mp4", user_id="u1")
        async with app.test_client() as client:
            resp = await client.get(f"/f/{token}/raw")
        assert resp.status_code == 200
        assert await resp.get_data() == b"0123456789"
        assert resp.headers["Accept-Ranges"] == "bytes"


class TestRawExpiredObject:
    """P11-G4-4: a delivered document the bucket's lifecycle already deleted, behind a token that is still valid.
    The real GcsMediaAdapter over the GCS fake: the object is gone, so the route says so (410), not "unavailable"."""

    async def test_expired_object_answers_410_not_503(self, tokens, kit, monkeypatch, caplog):
        import logging

        from src.adapters.gcs_media_adapter import GcsMediaAdapter
        from tests.support.gcs_fake import FakeGcs

        fake = FakeGcs(buckets={"media-test": "plain"})
        fake.install(monkeypatch)
        app = _app(tokens, GcsMediaAdapter("media-test"), kit)
        token = tokens.mint(key="video_generation/u1/ts.mp4", user_id="u1")
        with caplog.at_level(logging.INFO):
            async with app.test_client() as client:
                resp = await client.get(f"/f/{token}/raw")
        assert resp.status_code == 410
        assert await resp.get_data(as_text=True) == "This file has expired."
        assert not any(r.levelno >= logging.ERROR for r in caplog.records)
