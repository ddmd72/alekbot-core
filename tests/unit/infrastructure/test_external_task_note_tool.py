"""`create_external_task` and the task's note (owner's decision 2026-11-24): the declaration asks for it, the handler
refuses a create without it or with one too long, and a reminder's create posts its show-only notice itself.

The handler over the real service on the fakes of `tests/support/external_task_world.py`. Assertions are on what the
stores hold, the text the model reads and the receipt the owner's channel gets (a chat create's delivery item, a
reminder create's posted receipt). Alex Doe is `usr-1` with the
worker `research`.
"""
import pytest

from src.domain.external_task import NOTE_MAX_CHARS, TASK_TEXT_MAX_CHARS, ExternalTaskStatus
from src.domain.external_task_entry import TaskEntry
from src.domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY
from src.domain.external_task_texts import (
    ERROR_ARGUMENTS, ERROR_BRIEF_TOO_LONG, ERROR_INVALID_TEXT, ERROR_NOTE_TOO_LONG, ERROR_TITLE_TOO_LONG,
    NOTE_PARAMETER_TEXT, NOTE_QUEUED,
)
from src.domain.llm import ToolCall
from src.infrastructure.external_task_tools import build_create_external_task_declaration, make_create_handler
from tests.support.external_task_world import NOTE, ORIGIN, TEXT, TITLE, USER_ID, ExternalTaskWorld

S = ExternalTaskStatus


@pytest.fixture
async def world():
    return await ExternalTaskWorld().seed()


MISSING = object()


async def create(world, entry=TaskEntry.CHAT, post_receipt=None, **over):
    """A create through the handler; an argument given as MISSING is left out of the call."""
    handler = make_create_handler(world.service, USER_ID, ORIGIN, entry=entry, post_receipt=post_receipt)
    args = {k: v for k, v in {"worker": "research", "title": TITLE, "text": TEXT, "note": NOTE, **over}.items()
            if v is not MISSING}
    return await handler(ToolCall(name="create_external_task", args=args))


@pytest.mark.parametrize("entry", list(TaskEntry))
async def test_the_declaration_requires_the_note_and_explains_it(world, entry):
    declaration = build_create_external_task_declaration(await world.service.list_workers(USER_ID), entry=entry)
    assert "note" in declaration["parameters"]["required"]
    assert declaration["parameters"]["properties"]["note"] == {"type": "string", "description": NOTE_PARAMETER_TEXT}
    assert "never sees the note" in declaration["description"]


@pytest.mark.parametrize("args,line", [
    ({"note": MISSING}, ERROR_ARGUMENTS),
    ({"note": 5}, ERROR_ARGUMENTS),
    ({"note": "  "}, ERROR_INVALID_TEXT),
    ({"note": "n" * (NOTE_MAX_CHARS + 1)}, ERROR_NOTE_TOO_LONG),
    ({"note": "n" * (NOTE_MAX_CHARS + 1), "title": "t" * 500}, ERROR_TITLE_TOO_LONG),
    ({"note": "n" * (NOTE_MAX_CHARS + 1), "text": TEXT * (TASK_TEXT_MAX_CHARS // len(TEXT) + 1)}, ERROR_BRIEF_TOO_LONG),
])
async def test_a_missing_empty_or_long_note_is_a_tool_error_and_stores_nothing(world, args, line):
    result = await create(world, **args)
    assert result.failed and result.result_str == line and result.delivery_items == []
    assert world.tasks.tasks == {}


async def test_a_chat_create_puts_the_note_in_the_receipt_item(world):
    result = await create(world)
    [task] = world.tasks.tasks.values()
    [item] = result.delivery_items
    assert item.type == EXTERNAL_TASK_RECEIPT_DELIVERY and item.data["note"] == NOTE == task.note
    assert task.entry is TaskEntry.CHAT and item.data["command"].startswith("$task send ")
    assert NOTE not in result.result_str


async def test_a_reminders_create_posts_a_show_only_notice_with_no_command_and_returns_no_item(world):
    posted = []

    async def post_receipt(data):
        posted.append((dict(data), {i: t.status for i, t in world.tasks.tasks.items()}))   # and the store as it posted
        return True

    result = await create(world, entry=TaskEntry.REMINDER, post_receipt=post_receipt)
    [task] = world.tasks.tasks.values()
    assert task.status is S.QUEUED and task.entry is TaskEntry.REMINDER and task.note == NOTE
    assert result.result_str == NOTE_QUEUED.format(task_id=task.task_id, worker="research", title=TITLE)
    assert result.delivery_items == []                              # posted here, so never again by the caller
    [(data, stored)] = posted                                       # once, after the task was queued
    assert stored == {task.task_id: S.QUEUED}
    assert data == {"status": "queued", "worker": "research", "title": TITLE, "text": TEXT, "note": NOTE,
                    "task_id": task.task_id, "command": None}
