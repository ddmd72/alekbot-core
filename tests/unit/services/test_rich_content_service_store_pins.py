"""P11 card C, caller pin: the real RichContentService over the real GcsMediaAdapter on the shared GCS fake
(tests/support/gcs_fake.py). The platform boundary is a recording PlatformMediaPort. P11-G4-5: since the media bucket
went private, `store()` returns the object key, and an HTML rich-content file reaches the user as that bare key."""
from dataclasses import dataclass
from typing import List
from urllib.parse import urlparse

import pytest

from src.adapters.gcs_media_adapter import GcsMediaAdapter
from src.domain.messaging import RichContent
from src.ports.platform_media_port import PlatformMediaPort
from src.services.rich_content_service import RichContentService
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


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P11-G4-5: an HTML rich-content file is posted as its storage key")
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
