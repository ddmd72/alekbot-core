"""Mutation-survivor tests for ConversationHandler._deliver_item, `document` branch.

Pins that the owner's user_id reaches DocumentDeliveryService.store (the document is
stored under that user) and that the thread_id reaches ResponseChannel.send_file (the
uploaded file lands in the same thread as the link).
"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock

from src.domain.agent import DeliveryItem
from src.handlers.conversation_handler import ConversationHandler
from src.services.document_delivery_service import DeliveredDocument


class _FakeDocService:
    """Stores documents per user, like the real service keys objects by user."""

    def __init__(self) -> None:
        self.stored: dict[str, list[tuple[str, bytes, str, str]]] = {}

    async def store(self, content, filename, content_type, *, user_id, storage_class="document"):
        self.stored.setdefault(user_id, []).append((filename, content, content_type, storage_class))
        return DeliveredDocument(link=f"https://files.invalid/{user_id}/{filename}", key=f"docs/{user_id}/{filename}")


class _FakeChannel:
    def __init__(self) -> None:
        self.links: list[dict] = []
        self.files: list[dict] = []

    async def send_document_link(self, *, url, label, thread_id=None):
        self.links.append({"url": url, "label": label, "thread_id": thread_id})

    async def send_file(self, *, content, filename, title=None, thread_id=None):
        self.files.append({"content": content, "filename": filename, "title": title, "thread_id": thread_id})


def _handler(doc_service) -> ConversationHandler:
    return ConversationHandler(
        coordinator=MagicMock(),
        agent_factory=MagicMock(),
        file_service=MagicMock(),
        doc_delivery_service=doc_service,
    )


def _item(**extra) -> DeliveryItem:
    data = {
        "content_b64": base64.b64encode(b"payload").decode(),
        "filename": "file-1.txt",
        "content_type": "text/plain",
        "label": "File one",
        **extra,
    }
    return DeliveryItem(type="document", data=data)


async def test_document_is_stored_under_the_requesting_user():
    docs, channel = _FakeDocService(), _FakeChannel()

    await _handler(docs)._deliver_item(_item(), channel, thread_id="thread-1", user_id="alpha-user")

    assert docs.stored == {"alpha-user": [("file-1.txt", b"payload", "text/plain", "document")]}
    assert channel.links == [
        {"url": "https://files.invalid/alpha-user/file-1.txt", "label": "File one", "thread_id": "thread-1"}
    ]


async def test_document_file_upload_goes_to_the_same_thread():
    docs, channel = _FakeDocService(), _FakeChannel()

    await _handler(docs)._deliver_item(
        _item(file_upload=True), channel, thread_id="thread-1", user_id="alpha-user"
    )

    assert channel.files == [
        {"content": b"payload", "filename": "file-1.txt", "title": "File one", "thread_id": "thread-1"}
    ]
