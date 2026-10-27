"""P11 card C: the real GcsMediaAdapter behind MediaStoragePort, over the shared GCS fake (tests/support/gcs_fake.py,
installed as `google.cloud.storage.Client`, since the adapter builds a client per call). Private writes, the noindex
meta, overwrite, reads, the signing request, and P11-G4-4 (fetch of a missing key)."""
from datetime import timedelta

import google.auth
import google.auth.credentials
import google.auth.exceptions
import pytest
from google.api_core.exceptions import ServiceUnavailable

from src.adapters.gcs_media_adapter import GcsMediaAdapter
from tests.support.gcs_fake import FakeGcs
from tests.support.redelivery_world import require

BUCKET = "media-test"
KEY = "docs/u1/0f6c1a2e-FA-0137.pdf"
ROBOTS = b'<meta name="robots" content="noindex, nofollow">'


def _adapter(monkeypatch, fake: FakeGcs, service_account_email=None) -> GcsMediaAdapter:
    fake.install(monkeypatch)
    return GcsMediaAdapter(BUCKET, service_account_email=service_account_email)


@pytest.fixture
def fake():
    return FakeGcs(buckets={BUCKET: "plain"})


@pytest.fixture
def store(monkeypatch, fake):
    return _adapter(monkeypatch, fake)


class _KeyCredentials(google.auth.credentials.Signing):
    """A credential that signs locally (a service-account key file)."""

    def sign_bytes(self, message):  # pragma: no cover - the fake blob never signs
        return b""

    @property
    def signer_email(self):
        return "key-sa@example.com"

    @property
    def signer(self):
        return None


class _RuntimeCredentials:
    """Cloud Run metadata credentials: cannot sign locally; refresh resolves the email (no network here)."""

    def __init__(self, email):
        self.service_account_email, self.token = email, "access-token"

    def refresh(self, request):
        pass


async def test_store_returns_the_key_and_stores_privately(fake, store):
    assert await store.store(b"%PDF invoice FA-0137", KEY, "application/pdf") == KEY
    obj = fake.live(BUCKET, KEY)
    assert (obj.data, obj.content_type) == (b"%PDF invoice FA-0137", "application/pdf")
    # The fake refuses make_public, acl and predefined_acl with AssertionError: reaching here means none was asked.
    assert fake.names(BUCKET) == [KEY]


async def test_html_gets_noindex_once(fake, store):
    page = b"<html><HEAD></HEAD><body>Lisbon trip notes</body></html>"
    await store.store(page, "html/u1/trip-notes.html", "text/html; charset=utf-8")
    stored = fake.live(BUCKET, "html/u1/trip-notes.html").data
    assert stored.count(ROBOTS) == 1
    assert stored.startswith(b"<html><HEAD>\n    " + ROBOTS + b"</HEAD>")


async def test_html_without_head_is_stored_unchanged(fake, store):
    page = b"<html><body>Lisbon trip notes</body></html>"
    await store.store(page, "html/u1/trip-notes.html", "text/html; charset=utf-8")
    assert fake.live(BUCKET, "html/u1/trip-notes.html").data == page


async def test_non_html_is_never_rewritten(fake, store):
    text = b"<head></head> as text"
    await store.store(text, "docs/u1/notes.txt", "text/plain")
    assert fake.live(BUCKET, "docs/u1/notes.txt").data == text


async def test_store_overwrites_an_existing_key(fake, store):
    await store.store(b"first", KEY, "application/pdf")
    await store.store(b"second", KEY, "application/pdf")
    assert await store.fetch(KEY) == b"second"
    assert [v.data for v in fake.versions(BUCKET, KEY)] == [b"second"]


async def test_fetch_round_trip(store):
    await store.store(b"email review", "email_review/u1/5d2e-review.html", "text/plain")
    assert await store.fetch("email_review/u1/5d2e-review.html") == b"email review"


async def test_fetch_outage_propagates(fake, store):
    await store.store(b"x", KEY, "application/pdf")
    fake.fail_next("download", ServiceUnavailable("503"))
    with pytest.raises(ServiceUnavailable):
        await store.fetch(KEY)


async def test_store_outage_propagates_and_stores_nothing(fake, store):
    fake.fail_next("upload", ServiceUnavailable("503"))
    with pytest.raises(ServiceUnavailable):
        await store.store(b"x", KEY, "application/pdf")
    assert fake.names(BUCKET) == []


async def test_signed_url_with_a_local_signer_needs_no_object(monkeypatch, fake, store):
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (_KeyCredentials(), "proj"))
    url = await store.generate_signed_url(KEY, 300)
    assert url
    [request] = fake.signed_urls
    assert (request["name"], request["version"], request["method"], request["expiration"]) == (
        KEY, "v4", "GET", timedelta(seconds=300))
    assert request["service_account_email"] is None and request["access_token"] is None
    assert fake.names(BUCKET) == []                   # the key does not exist


async def test_signed_url_with_runtime_credentials_signs_through_iam(monkeypatch, fake, store):
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (_RuntimeCredentials("runtime-sa@example.com"), "p"))
    await store.generate_signed_url(KEY, 60)
    [request] = fake.signed_urls
    assert (request["service_account_email"], request["access_token"], request["expiration"]) == (
        "runtime-sa@example.com", "access-token", timedelta(seconds=60))


async def test_signed_url_without_ambient_credentials_lets_the_client_sign(monkeypatch, fake, store):
    def no_credentials(*args, **kwargs):
        raise google.auth.exceptions.DefaultCredentialsError("none")

    monkeypatch.setattr(google.auth, "default", no_credentials)
    assert await store.generate_signed_url(KEY, 60)
    [request] = fake.signed_urls
    assert request["service_account_email"] is None and request["expiration"] == timedelta(seconds=60)


async def test_signed_url_with_an_unknown_signer_is_a_runtime_error(monkeypatch, fake, store):
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (_RuntimeCredentials("default"), "proj"))
    with pytest.raises(RuntimeError):
        await store.generate_signed_url(KEY, 60)
    assert fake.signed_urls == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="P11-G4-4: fetch of a missing key leaks the SDK NotFound")
async def test_fetch_of_a_missing_key_is_file_not_found(store):
    """Delivered documents expire (email_review/ after 5 days, the rest after 30) while history keeps their keys, so
    `open_file` on last week's review takes the generic "fetch failed" branch with an ERROR instead of "expired".
    Flips when `fetch` maps NotFound to FileNotFoundError. Control: test_fetch_round_trip."""
    err = None
    try:
        await store.fetch("email_review/u1/5d2e-review.html")
    except Exception as exc:
        err = exc
    require(err is not None, "fetch of a missing key raised")
    assert isinstance(err, FileNotFoundError)
