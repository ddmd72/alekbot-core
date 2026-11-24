"""ConversationHandler: the external task receipt delivery and the `$task send` command
(docs/10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md §5.3).

The real `ExternalTaskService` over the fakes of `tests/support/external_task_world.py`: a held task is stored, and the
command queues it. Alex Doe is `usr-1`.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.external_task_entry import TaskEntry
from src.domain.agent import DeliveryItem
from src.domain.external_task import ExternalTaskStatus
from src.domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY, receipt_data
from src.domain.messaging import MessageContext
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.handlers.command_services import CommandServices
from src.handlers.conversation_handler import ConversationHandler
from src.locales import uk as uk_locale
from tests.support.external_task_world import ORIGIN, TEXT, TITLE, USER_ID, ExternalTaskWorld

S = ExternalTaskStatus


def _ui(message: UIMessage, **fmt) -> str:
    template = uk_locale.UI_STRINGS[message.value]
    return template.format(**fmt) if fmt else template


def _context(command: str) -> MessageContext:
    return MessageContext(text=command, session_id=f"{USER_ID}:C1", user_id=USER_ID, account_id="acc-1",
                          attachments=[], metadata={})


def _channel() -> MagicMock:
    ch = MagicMock()
    ch.send_message = AsyncMock()
    ch.send_literal = AsyncMock()
    ch.language = None
    return ch


def _handler(service) -> ConversationHandler:
    return ConversationHandler(
        coordinator=MagicMock(), agent_factory=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
        commands=CommandServices(external_tasks=service),
    )


def _sent(ch):
    return [c.args[0] for c in ch.send_message.await_args_list]


def _literal(ch):
    return [c.args[0] for c in ch.send_literal.await_args_list]


@pytest.fixture
async def world():
    return await ExternalTaskWorld().seed()


async def _held(world):
    created = await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.CHAT)
    return created


class TestReceiptDelivery:

    async def test_a_held_receipt_posts_the_text_literally_then_the_command_alone(self, world):
        created = await _held(world)
        ch = _channel()
        item = DeliveryItem(type=EXTERNAL_TASK_RECEIPT_DELIVERY, data=receipt_data(created))

        await _handler(world.service)._deliver_item(item, ch, "T1", USER_ID)

        [post] = _literal(ch)
        assert TEXT in post and TITLE in post and "research" in post and created.task_id in post
        assert created.code not in post
        assert _sent(ch) == [f"`$task send {created.code}`"]
        assert ch.send_literal.await_args.args[1] == "T1" and ch.send_message.await_args.args[1] == "T1"

    async def test_a_failed_receipt_never_sends_the_command(self, world):
        created = await _held(world)
        ch = _channel()
        ch.send_literal.side_effect = RuntimeError("slack down")
        item = DeliveryItem(type=EXTERNAL_TASK_RECEIPT_DELIVERY, data=receipt_data(created))

        await _handler(world.service)._deliver_item(item, ch, None, USER_ID)

        assert _sent(ch) == []


class TestTaskSend:

    async def test_the_command_queues_the_held_task_and_says_so(self, world):
        created = await _held(world)
        ch = _channel()

        await _handler(world.service).handle_command(f"task send {created.code}", _context("task send"), ch)

        assert world.tasks.tasks[created.task_id].status is S.QUEUED
        assert _literal(ch) == [_ui(UIMessage.EXTERNAL_TASK_SENT, title=TITLE, worker="research")]

    async def test_a_wrong_code_gets_one_neutral_line_and_changes_nothing(self, world):
        created = await _held(world)
        ch = _channel()

        await _handler(world.service).handle_command("task send 00000000", _context("task send"), ch)

        assert world.tasks.tasks[created.task_id].status is S.HELD
        assert _sent(ch) == [_ui(UIMessage.EXTERNAL_TASK_NOT_FOUND)]

    async def test_a_used_code_is_the_same_neutral_line(self, world):
        created = await _held(world)
        handler = _handler(world.service)
        await handler.handle_command(f"task send {created.code}", _context("task send"), _channel())
        ch = _channel()

        await handler.handle_command(f"task send {created.code}", _context("task send"), ch)

        assert _sent(ch) == [_ui(UIMessage.EXTERNAL_TASK_NOT_FOUND)]

    async def test_the_flag_is_read_fresh_so_a_switched_off_user_gets_unavailable(self, world):
        created = await _held(world)
        await world.flag(USER_ID, False)
        ch = _channel()

        await _handler(world.service).handle_command(f"task send {created.code}", _context("task send"), ch)

        assert world.tasks.tasks[created.task_id].status is S.HELD
        assert _sent(ch) == [_ui(UIMessage.EXTERNAL_TASK_UNAVAILABLE)]

    async def test_another_users_code_does_not_queue_the_task(self, world):
        created = await _held(world)
        ch = _channel()
        context = MessageContext(text="x", session_id="usr-2:C1", user_id="usr-2", account_id="acc-2",
                                 attachments=[], metadata={})

        await _handler(world.service).handle_command(f"task send {created.code}", context, ch)

        assert world.tasks.tasks[created.task_id].status is S.HELD
        assert _sent(ch) == [_ui(UIMessage.EXTERNAL_TASK_NOT_FOUND)]

    async def test_a_service_failure_says_unavailable_and_never_escapes(self, world):
        service = MagicMock()
        service.confirm = AsyncMock(side_effect=RuntimeError("firestore down"))
        ch = _channel()

        await _handler(service).handle_command("task send ab12cd34", _context("task send"), ch)

        assert _sent(ch) == [_ui(UIMessage.EXTERNAL_TASK_UNAVAILABLE)]

    async def test_bare_or_malformed_command_prints_usage(self, world):
        handler = _handler(world.service)
        for command in ("task", "task send", "task list", "task send a b"):
            ch = _channel()
            await handler.handle_command(command, _context(command), ch)
            assert _sent(ch) == [_ui(UIMessage.EXTERNAL_TASK_USAGE)]

    async def test_no_service_configured(self):
        ch = _channel()

        await _handler(None).handle_command("task send ab12cd34", _context("task send"), ch)

        assert _sent(ch) == [_ui(UIMessage.EXTERNAL_TASK_UNAVAILABLE)]
