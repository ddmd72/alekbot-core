"""The file-link and file-access log lines name an object by the owner's id prefix and a fingerprint of its key."""
import logging

import google.auth
import pytest
from quart import Quart

from src.adapters.gcs_media_adapter import GcsMediaAdapter
from src.domain.secret_digest import fingerprint
from src.services.file_access_token_service import FileAccessTokenService
from src.services.file_link_service import FileLinkService
from src.web.file_access_app import create_file_access_blueprint
from tests.support.gcs_fake import FakeGcs
from tests.unit.adapters.test_media_store_kit import _KeyCredentials

BUCKET = "media-test"
KEY = "email_review/u1/5d2e-review.html"


@pytest.fixture
def tokens():
    return FileAccessTokenService(secret_key="q" * 40)


@pytest.fixture
def fake(monkeypatch):
    gcs = FakeGcs(buckets={BUCKET: "plain"})
    gcs.install(monkeypatch)
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (_KeyCredentials(), "proj"))
    return gcs


def test_building_a_link_logs_the_fingerprint_not_the_key(tokens, caplog):
    with caplog.at_level(logging.INFO):
        FileLinkService(token_service=tokens, base_url="https://dev.alekbot.app").build_link(key=KEY, user_id="u1")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert fingerprint(KEY) in text and KEY not in text


async def test_a_missing_object_logs_the_fingerprint_not_the_key(tokens, fake, caplog):
    app = Quart("file_access_log_keys")
    app.register_blueprint(create_file_access_blueprint(
        token_service=tokens, media_storage=GcsMediaAdapter(BUCKET), cabinet_sessions=None))
    token = tokens.mint(key=KEY, user_id="u1")
    with caplog.at_level(logging.INFO):
        assert (await app.test_client().get(f"/f/{token}")).status_code == 410
        assert (await app.test_client().get(f"/f/{token}/raw")).status_code == 410
    text = " ".join(r.getMessage() for r in caplog.records if r.getMessage().startswith("[FileAccess]"))
    assert f"user=u1 key={fingerprint(KEY)}" in text and KEY not in text
