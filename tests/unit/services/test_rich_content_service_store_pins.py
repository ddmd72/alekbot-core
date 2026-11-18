"""P11 card C, caller pin: the real RichContentService over the real GcsMediaAdapter on the shared GCS fake
(tests/support/gcs_fake.py). The platform boundary is a recording PlatformMediaPort. P11-G4-5: since the media bucket
went private, `store()` returns the object key, and an HTML rich-content file reaches the user as that bare key."""
import logging
from dataclasses import dataclass
from typing import List
from urllib.parse import urlparse

import google.auth
import google.auth.exceptions
import pytest
from google.api_core.exceptions import ServiceUnavailable
from quart import Quart

from src.adapters.gcs_media_adapter import GcsMediaAdapter
from src.domain.messaging import RichContent
from src.ports.platform_media_port import PlatformMediaPort
from src.services.file_access_token_service import FileAccessTokenService
from src.services.file_link_service import FileLinkService
from src.services.rich_content_service import RichContentService
from src.web.file_access_app import create_file_access_blueprint
from tests.support.gcs_fake import FakeGcs
from tests.support.redelivery_world import require

BUCKET = "media-test"
PAGE = "<html><head></head><body>Lisbon trip notes</body></html>"


@dataclass(frozen=True)
class Upload:
    file_bytes: bytes
    filename: str
    title: str
    channel_id: str


class RecordingPlatform(PlatformMediaPort):
    """What the platform received, in order."""

    def __init__(self) -> None:
        self.files: List[Upload] = []
        self.images: List[tuple] = []

    async def upload_image(self, image_bytes: bytes, alt_text: str, channel_id: str) -> None:
        self.images.append((image_bytes, alt_text, channel_id))

    async def upload_file(self, file_bytes: bytes, filename: str, title: str, channel_id: str) -> None:
        self.files.append(Upload(file_bytes, filename, title, channel_id))


def _service(monkeypatch, fake: FakeGcs, platform: RecordingPlatform) -> RichContentService:
    """The one place the service is built (as the Slack factory builds it: with the storage port). A fix that changes
    the constructor or `process` touches this helper and the two `process` calls, nothing else."""
    fake.install(monkeypatch)
    return RichContentService(media_port=platform, storage_port=GcsMediaAdapter(BUCKET))


@pytest.fixture
def fake():
    return FakeGcs(buckets={BUCKET: "plain"})


async def test_markdown_file_is_uploaded_to_the_platform(monkeypatch, fake):
    platform = RecordingPlatform()
    service = _service(monkeypatch, fake, platform)
    out = await service.process(RichContent("file", {"filename": "trip-notes.md", "content": "# Lisbon trip"}, ""), "C1")
    assert out is None
    assert platform.files == [Upload(b"# Lisbon trip", "trip-notes.md", "trip-notes.md", "C1")]
    assert fake.names(BUCKET) == []


async def test_html_file_reaches_the_user_as_a_link(monkeypatch, fake):
    """`_store_html` returns what `store()` returns, the key `html/<uuid>-trip-notes.html` since the bucket went private,
    and ConversationHandler posts it as the link: on Slack the user gets bare text, not a link. Flips when the page
    goes through DocumentDeliveryService and `process` returns its `.link` (needs the user id: `_service()` and the
    call change), or when the page is uploaded to the platform as a file, as on Telegram. A link counts only when the
    private bucket serves it: a V4 signed URL (`X-Goog-Signature`) or the app's own capability route `/f/<token>`; the
    pre-2026-06-01 public storage URL does not open and does not count. Control:
    test_markdown_file_is_uploaded_to_the_platform."""
    platform = RecordingPlatform()
    service = _service(monkeypatch, fake, platform)
    content = RichContent("file", {"filename": "trip-notes.html", "content": PAGE}, "")
    require(content.data["content"] and content.data["filename"].endswith(".html"), "an HTML file with content")
    out = await service.process(content, "C1")
    as_link = isinstance(out, str) and out.startswith(("https://", "http://")) and (
        "X-Goog-Signature=" in out or urlparse(out).path.startswith("/f/"))
    as_file = out is None and any(u.filename == "trip-notes.html" for u in platform.files)
    assert as_link or as_file


BASE = "https://alek.example.com"
SECRET = "s" * 40


def _linking_service(monkeypatch, fake: FakeGcs, platform: RecordingPlatform):
    """As the Slack kit is built in production (notification_setup): the media bucket and the `/f/<token>` link service."""
    fake.install(monkeypatch)
    tokens = FileAccessTokenService(secret_key=SECRET)
    links = FileLinkService(token_service=tokens, base_url=BASE)
    return RichContentService(media_port=platform, storage_port=GcsMediaAdapter(BUCKET), link_service=links), tokens


async def test_html_file_for_a_user_is_a_capability_link_the_file_route_serves(monkeypatch, fake):
    """P11-G4-5 (O8): with the owner known, the page is stored privately and reaches the user as `/f/<token>`, the one
    delivery path of every stored file; following the link redirects to a signed URL for that very object."""
    platform = RecordingPlatform()
    service, tokens = _linking_service(monkeypatch, fake, platform)
    out = await service.process(RichContent("file", {"filename": "trip-notes.html", "content": PAGE}, ""), "C1",
                                user_id="u1")
    assert isinstance(out, str) and out.startswith(f"{BASE}/f/") and platform.files == []
    access = tokens.verify(out.rsplit("/", 1)[-1])
    assert access.user_id == "u1" and not access.gated
    assert access.key.startswith("html/") and access.key.endswith("-trip-notes.html")
    stored = fake.live(BUCKET, access.key)
    assert stored is not None and b"Lisbon trip notes" in stored.data and b'name="robots"' in stored.data

    def no_ambient_credentials(*args, **kwargs):     # the fake client signs; no IAM signer in a unit test
        raise google.auth.exceptions.DefaultCredentialsError("none")

    monkeypatch.setattr(google.auth, "default", no_ambient_credentials)
    app = Quart("files")
    app.register_blueprint(create_file_access_blueprint(token_service=tokens, media_storage=GcsMediaAdapter(BUCKET),
                                                        session_service=None))  # type: ignore[arg-type]
    async with app.test_client() as client:
        resp = await client.get(urlparse(out).path)
    assert resp.status_code == 302 and "X-Goog-Signature=" in resp.headers["Location"]
    assert fake.signed_urls[-1]["name"] == access.key


async def test_html_file_without_a_user_is_uploaded_as_a_file_and_not_stored(monkeypatch, fake):
    """P11-G4-5: no owner, no link (a token names its user): the page goes to the platform as a file, as on Telegram,
    and nothing is left in the bucket."""
    platform = RecordingPlatform()
    service, _ = _linking_service(monkeypatch, fake, platform)
    out = await service.process(RichContent("file", {"filename": "trip-notes.html", "content": PAGE}, ""), "C1")
    assert out is None and [u.filename for u in platform.files] == ["trip-notes.html"]
    assert fake.names(BUCKET) == []


async def test_html_link_store_failure_delivers_nothing_and_says_so(monkeypatch, fake, caplog):
    """P11-G4-5: the upload fails: no link (and no key) goes out, and the failure is an ERROR."""
    platform = RecordingPlatform()
    service, _ = _linking_service(monkeypatch, fake, platform)
    fake.fail_next("upload", ServiceUnavailable("503"))
    with caplog.at_level(logging.ERROR):
        out = await service.process(RichContent("file", {"filename": "trip-notes.html", "content": PAGE}, ""), "C1",
                                    user_id="u1")
    assert out is None and platform.files == []
    assert any(r.levelno == logging.ERROR and "trip-notes.html" in r.getMessage() for r in caplog.records)


async def test_store_html_without_a_link_service_stores_nothing(monkeypatch, fake, caplog):
    """The `html_gcs_link` delivery item calls `_store_html`: without a way to link the page nothing is stored and
    None comes back (the caller posts nothing), never the storage key."""
    platform = RecordingPlatform()
    service = _service(monkeypatch, fake, platform)
    with caplog.at_level(logging.WARNING):
        assert await service._store_html(PAGE, "trip-notes.html", "u1") is None
    assert fake.names(BUCKET) == []
    assert any("no capability link" in r.getMessage() for r in caplog.records)
