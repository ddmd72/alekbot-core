"""RichContentService.deliver: the one routing of a rich-content item, shared by chat turns and background notifications."""
from src.domain.messaging import RichContent
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
    async def store(self, data, key, content_type):
        return f"https://storage.example.com/{key}"


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
    media, channel = FakeMedia(), FakeChannel()
    await RichContentService(media_port=media, storage_port=FakeStorage()).deliver(_rc("file", HTML), channel, "t1")
    assert media.files == [] and channel.rich == []
    assert len(channel.messages) == 1
    assert channel.messages[0][0].startswith("https://storage.example.com/html/") and channel.messages[0][1] == "t1"


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
