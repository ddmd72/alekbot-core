"""`/f/<token>` for an object the bucket's lifecycle already deleted, behind a token that is still valid.

The redirect used to sign a URL for the missing key and send the browser to it, where Google Cloud Storage answers a raw
NoSuchKey XML page. It now asks the store first and answers what `/f/<token>/raw` answers for the same case (P11-G4-4):
410 "This file has expired.". The real GcsMediaAdapter over the GCS fake, as `TestRawExpiredObject` builds it; the
adapter's `exists` is pinned in the media kit's terms here too.
"""
import logging

import google.auth
import pytest
from google.api_core.exceptions import ServiceUnavailable
from quart import Quart

from src.adapters.gcs_media_adapter import GcsMediaAdapter
from src.services.file_access_token_service import FileAccessTokenService
from src.web.file_access_app import create_file_access_blueprint
from tests.support.gcs_fake import FakeGcs
from tests.unit.adapters.test_media_store_kit import _KeyCredentials

BUCKET = "media-test"
KEY = "email_review/u1/5d2e-review.html"
VIDEO_KEY = "video_generation/u1/1760000000.mp4"


@pytest.fixture
def tokens():
    return FileAccessTokenService(secret_key="q" * 40)


@pytest.fixture
def fake(monkeypatch):
    gcs = FakeGcs(buckets={BUCKET: "plain"})
    gcs.install(monkeypatch)
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (_KeyCredentials(), "proj"))
    return gcs


def _client(tokens):
    app = Quart("file_access_expired")
    app.register_blueprint(create_file_access_blueprint(
        token_service=tokens, media_storage=GcsMediaAdapter(BUCKET), session_service=None))
    return app.test_client()


async def test_an_expired_object_answers_410_and_signs_nothing(tokens, fake, caplog):
    """Flips when the redirect signs without asking whether the object is still stored."""
    token = tokens.mint(key=KEY, user_id="u1")
    with caplog.at_level(logging.INFO):
        resp = await _client(tokens).get(f"/f/{token}")
    assert resp.status_code == 410
    assert await resp.get_data(as_text=True) == "This file has expired."
    assert fake.signed_urls == []
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_an_expired_video_object_answers_410_not_a_player_page(tokens, fake):
    """Flips when a video key gets the landing page without the existence check other keys get."""
    token = tokens.mint(key=VIDEO_KEY, user_id="u1")
    resp = await _client(tokens).get(f"/f/{token}")
    assert resp.status_code == 410
    assert await resp.get_data(as_text=True) == "This file has expired."
    assert fake.signed_urls == []


async def test_a_stored_video_object_still_gets_the_player_page(tokens, fake):
    await GcsMediaAdapter(BUCKET).store(b"video bytes", VIDEO_KEY, "video/mp4")
    token = tokens.mint(key=VIDEO_KEY, user_id="u1")
    resp = await _client(tokens).get(f"/f/{token}")
    assert resp.status_code == 200
    assert f'<video controls playsinline src="/f/{token}/raw">' in await resp.get_data(as_text=True)


async def test_a_stored_object_still_redirects_to_its_signed_url(tokens, fake):
    await GcsMediaAdapter(BUCKET).store(b"<html><head></head></html>", KEY, "text/html; charset=utf-8")
    token = tokens.mint(key=KEY, user_id="u1")
    resp = await _client(tokens).get(f"/f/{token}")
    assert resp.status_code == 302
    [request] = fake.signed_urls
    assert request["name"] == KEY


async def test_a_storage_outage_on_the_check_answers_503_and_signs_nothing(tokens, fake):
    fake.fail_next("exists", ServiceUnavailable("gcs down"))
    token = tokens.mint(key=KEY, user_id="u1")
    resp = await _client(tokens).get(f"/f/{token}")
    assert resp.status_code == 503
    assert fake.signed_urls == []


async def test_exists_is_true_for_a_stored_key_and_false_for_a_missing_one(fake):
    store = GcsMediaAdapter(BUCKET)
    await store.store(b"x", KEY, "text/plain")
    assert await store.exists(KEY) is True
    assert await store.exists("email_review/u1/never-written.html") is False


async def test_exists_propagates_a_storage_outage(fake):
    fake.fail_next("exists", ServiceUnavailable("gcs down"))
    with pytest.raises(ServiceUnavailable):
        await GcsMediaAdapter(BUCKET).exists(KEY)
