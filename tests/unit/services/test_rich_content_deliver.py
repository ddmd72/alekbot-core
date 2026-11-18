"""RichContentService.deliver: the one routing of a rich-content item, shared by chat turns and background notifications."""
from src.domain.messaging import RichContent
from src.services.file_access_token_service import FileAccessTokenService
from src.services.file_link_service import FileLinkService
from src.services.rich_content_service import RichContentService

FILE = {"filename": "rome_hotel.txt", "title": "Rome hotel booking",
        "content": "Albergo Immaginario, Via Finta 7, Rome. Booking RM-55120, 10-14 Aug 2025, 560 EUR."}
HTML = {"filename": "rome_trip.html", "title": "Rome trip", "content": "<h1>Rome</h1>"}


class FakeMedia:
    def __init__(self):
        self.files, self.images = [], []

    async def upload_file(self, file_bytes, filename, title, channel_id):
        self.files.append((file_bytes, filename, title, channel_id))

    async def upload_image(self, image_bytes, alt_text, channel_id):
        self.images.append((image_bytes, alt_text, channel_id))


class FakeStorage:
    """The private media bucket: store() keeps the object and returns its key (never a URL)."""

    def __init__(self):
        self.objects = {}

    async def store(self, data, key, content_type):
        self.objects[key] = data
        return key


BASE = "https://alek.example.com"
TOKENS = FileAccessTokenService(secret_key="s" * 40)
LINKS = FileLinkService(token_service=TOKENS, base_url=BASE)


class FakeChannel:
    def __init__(self, channel_id="D1"):
        self.channel_id = channel_id
        self.messages, self.rich = [], []

    async def send_message(self, text, thread_id=None, **kw):
        self.messages.append((text, thread_id))

    async def send_rich_content(self, content, thread_id=None):
        self.rich.append(content)


def _rc(kind, data, fallback="fallback"):
    return RichContent(content_type=kind, data=data, fallback_text=fallback)


async def test_file_is_uploaded_and_no_fallback_text_is_sent():
    media, channel = FakeMedia(), FakeChannel()
    await RichContentService(media_port=media).deliver(_rc("file", FILE), channel)
    assert media.files == [(FILE["content"].encode(), "rome_hotel.txt", "Rome hotel booking", "D1")]
    assert channel.rich == [] and channel.messages == []


async def test_html_file_with_storage_sends_the_url_as_a_message_in_the_thread():
    """P11-G4-5 (O8): the url is the owner's `/f/<token>` link to the stored page, never the storage key."""
    media, channel, storage = FakeMedia(), FakeChannel(), FakeStorage()
    service = RichContentService(media_port=media, storage_port=storage, link_service=LINKS)
    await service.deliver(_rc("file", HTML), channel, "t1", user_id="u1")
    assert media.files == [] and channel.rich == []
    [(link, thread)] = channel.messages
    assert link.startswith(f"{BASE}/f/") and thread == "t1"
    access = TOKENS.verify(link.rsplit("/", 1)[-1])
    assert access.user_id == "u1" and access.key.startswith("html/")
    assert storage.objects == {access.key: b"<h1>Rome</h1>"}


async def test_html_file_with_storage_but_no_user_is_uploaded_as_a_file_in_the_channel():
    """P11-G4-5: no owner to mint a link for: the page goes out as a file, nothing is stored, no key is posted."""
    media, channel, storage = FakeMedia(), FakeChannel(), FakeStorage()
    service = RichContentService(media_port=media, storage_port=storage, link_service=LINKS)
    await service.deliver(_rc("file", HTML), channel, "t1")
    assert media.files == [(b"<h1>Rome</h1>", "rome_trip.html", "Rome trip", "D1")]
    assert storage.objects == {} and channel.messages == [] and channel.rich == []


async def test_table_goes_to_the_channel_renderer():
    media, channel = FakeMedia(), FakeChannel()
    table = _rc("table", {"title": "T", "headers": ["A"], "rows": [{"cells": ["v"]}]})
    await RichContentService(media_port=media).deliver(table, channel)
    assert channel.rich == [table] and media.files == []


async def test_media_type_without_a_channel_id_falls_back_to_the_renderer():
    media, channel = FakeMedia(), FakeChannel(channel_id="")
    item = _rc("file", FILE)
    await RichContentService(media_port=media).deliver(item, channel)
    assert channel.rich == [item] and media.files == []
