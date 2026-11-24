"""ConversationHandler posts a literal notice the same way for the memory-fact preview and the external task receipt:
the exact text as a file, then the notice's message literally, then the command alone. A failed file or message sends no
command: the owner never confirms text they were not shown.

The real `ExternalTaskService` over the fakes of `tests/support/external_task_world.py`; Alex Doe is `usr-1`. A new
file beside `test_conversation_handler_task_command.py` / `test_conversation_handler_memory_command.py`, whose delivery
tests pin the earlier form and wait for the reviewer (root CLAUDE.md, reviewer rule)."""
from __future__ import annotations

from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import DeliveryItem
from src.domain.external_task_entry import TaskEntry
from src.domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY, receipt_data
from src.domain.memory_fact import MEMORY_PREVIEW_DELIVERY, MemoryFactDraft, ProposedFact, render_preview_file
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.handlers.command_services import CommandServices
from src.handlers.conversation_handler import ConversationHandler
from src.locales import uk as uk_locale
from tests.support.external_task_world import ORIGIN, TEXT, TITLE, USER_ID, ExternalTaskWorld

FACT = {"text": "User's passport expires on 2031-03-04.", "source": "passport.pdf"}


def _ui(message: UIMessage, **fmt) -> str:
    return uk_locale.UI_STRINGS[message.value].format(**fmt)


class _Channel:
    """Records every post in order as (kind, text, thread); `fail` names the kind that raises."""

    def __init__(self, fail: str = "") -> None:
        self.posts: List[Tuple[str, str, object]] = []
        self.files: List[dict] = []
        self.fail = fail
        self.language = None
        self.platform = "telegram"

    async def _post(self, kind: str, text: str, thread) -> None:
        if kind == self.fail:
            raise RuntimeError(f"{kind} down")
        self.posts.append((kind, text, thread))

    async def send_file(self, *, content: bytes, filename: str, title: str, thread_id=None) -> None:
        self.files.append({"content": content, "filename": filename, "title": title})
        await self._post("file", filename, thread_id)

    async def send_literal(self, text: str, to_message_id=None) -> None:
        await self._post("literal", text, to_message_id)

    async def send_message(self, text: str, thread_id=None) -> None:
        await self._post("message", text, thread_id)


def _handler(service=None) -> ConversationHandler:
    return ConversationHandler(
        coordinator=MagicMock(), agent_factory=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
        commands=CommandServices(external_tasks=service, memory_facts=MagicMock(confirm=AsyncMock())),
    )


@pytest.fixture
async def world():
    return await ExternalTaskWorld().seed()


async def _receipt(world) -> Tuple[DeliveryItem, str, str]:
    created = await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.CHAT)
    return DeliveryItem(type=EXTERNAL_TASK_RECEIPT_DELIVERY, data=receipt_data(created)), created.task_id, created.code


async def test_a_held_receipt_is_the_text_file_then_the_literal_message_then_the_command(world):
    item, task_id, code = await _receipt(world)
    ch = _Channel()

    await _handler(world.service)._deliver_item(item, ch, "T1", USER_ID)

    assert [(kind, thread) for kind, _, thread in ch.posts] == [("file", "T1"), ("literal", "T1"), ("message", "T1")]
    [file] = ch.files
    assert file["content"] == TEXT.encode("utf-8") and file["filename"] == f"task-{task_id}.txt"
    assert file["title"] == TITLE
    message = ch.posts[1][1]
    assert TITLE in message and "research" in message and task_id in message
    assert TEXT not in message and code not in message
    assert ch.posts[2][1] == f"`$task send {code}`"


@pytest.mark.parametrize("fail", ["file", "literal"])
async def test_a_failed_receipt_file_or_message_never_sends_the_command(world, fail):
    item, _, _ = await _receipt(world)
    ch = _Channel(fail=fail)

    await _handler(world.service)._deliver_item(item, ch, None, USER_ID)

    assert not any(kind == "message" for kind, _, _ in ch.posts)


def _preview() -> DeliveryItem:
    return DeliveryItem(type=MEMORY_PREVIEW_DELIVERY, data={"facts": [FACT], "command": "$memory save ab12"})


async def test_the_memory_preview_is_the_facts_file_then_the_count_line_literally_then_the_command():
    ch = _Channel()

    await _handler()._deliver_item(_preview(), ch, "T1", USER_ID)

    assert ch.posts == [
        ("file", "memory-facts.txt", "T1"),
        ("literal", _ui(UIMessage.MEMORY_PREVIEW_HEADER, count=1), "T1"),
        ("message", "`$memory save ab12`", "T1"),
    ]
    assert ch.files[0]["content"] == render_preview_file(MemoryFactDraft(facts=[ProposedFact(**FACT)])).encode("utf-8")
    assert ch.files[0]["title"] == _ui(UIMessage.MEMORY_PREVIEW_FILE_TITLE, count=1)


@pytest.mark.parametrize("fail", ["file", "literal"])
async def test_a_failed_memory_preview_sends_the_failure_notice_and_never_the_command(fail):
    ch = _Channel(fail=fail)

    await _handler()._deliver_item(_preview(), ch, None, USER_ID)

    messages = [text for kind, text, _ in ch.posts if kind == "message"]
    assert messages == [_ui(UIMessage.MEMORY_PREVIEW_DELIVERY_FAILED)]
