"""ConversationHandler: on Slack the command message of a held task carries the «Send» button (EXTERNAL_TASK_QUEUE_RFC
§5.3); Telegram keeps the command alone.

The real `ExternalTaskService` over the fakes of `tests/support/external_task_world.py` makes the held task; the button
goes out through the real `InteractiveMessageRouter` and `SlackInteractiveMessageAdapter`, mocked at the slack_sdk client
boundary only. The channel's language is unset, so the strings are Ukrainian (the handler's default). Alex Doe is
`usr-1`."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.slack.binding_codec import decode
from src.adapters.slack.interactive_message_adapter import SlackInteractiveMessageAdapter
from src.domain.agent import DeliveryItem
from src.domain.external_task_entry import TaskEntry
from src.domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY, EXTERNAL_TASK_SEND_KIND, receipt_data
from src.domain.interactive_binding import InteractiveBinding
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.handlers.command_services import CommandServices
from src.handlers.conversation_handler import ConversationHandler
from src.infrastructure.interactive_message_router import InteractiveMessageRouter
from src.locales import uk as uk_locale
from tests.support.external_task_world import ORIGIN, TEXT, TITLE, USER_ID, ExternalTaskWorld, NOTE
from tests.support.task_receipt_file import receipt_parts

SEND = uk_locale.UI_STRINGS[UIMessage.EXTERNAL_TASK_SEND_BUTTON.value]


def _channel(platform: str = "slack") -> MagicMock:
    ch = MagicMock()
    ch.platform, ch.channel_id, ch.language = platform, "D1", None
    ch.send_message = AsyncMock()
    ch.send_literal = AsyncMock()
    ch.send_file = AsyncMock()   # the receipt's text file (ResponseChannel.send_file), posted before the literal message
    return ch


def _slack_client() -> MagicMock:
    c = MagicMock()
    c.chat_postMessage = AsyncMock(return_value={"ok": True, "channel": "D1", "ts": "1761295900.000200"})
    return c


def _handler(service, client: MagicMock | None) -> ConversationHandler:
    buttons = InteractiveMessageRouter({"slack": SlackInteractiveMessageAdapter(client)}) if client else None
    return ConversationHandler(
        coordinator=MagicMock(), agent_factory=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
        commands=CommandServices(external_tasks=service, task_send_buttons=buttons),
    )


@pytest.fixture
async def world():
    return await ExternalTaskWorld().seed()


async def _item(world) -> tuple:
    created = await world.service.create(USER_ID, "research", TITLE, TEXT, ORIGIN, entry=TaskEntry.CHAT, note=NOTE)
    return created, DeliveryItem(type=EXTERNAL_TASK_RECEIPT_DELIVERY, data=receipt_data(created))


async def test_on_slack_the_command_message_carries_the_send_button_after_the_literal_receipt(world):
    created, item = await _item(world)
    ch, client = _channel(), _slack_client()

    await _handler(world.service, client)._deliver_item(item, ch, None, USER_ID)

    [file] = [c.kwargs for c in ch.send_file.await_args_list]
    front, body = receipt_parts(file["content"])
    assert body == TEXT.encode("utf-8") and created.code.encode() not in file["content"]
    assert front == {"title": TITLE, "worker": "research", "note": NOTE}
    [post] = [c.args[0] for c in ch.send_literal.await_args_list]
    assert TITLE in post and created.code not in post
    ch.send_message.assert_not_awaited()
    posted = client.chat_postMessage.call_args.kwargs
    assert posted["channel"] == "D1" and "thread_ts" not in posted
    prompt, actions = posted["blocks"]
    assert prompt["text"]["text"] == f"`$task send {created.code}`"
    assert decode(actions["block_id"]) == InteractiveBinding(kind=EXTERNAL_TASK_SEND_KIND, id=created.task_id)
    [button] = actions["elements"]
    assert (button["action_id"], button["text"]["text"]) == (created.code, SEND)
    assert TEXT not in str(posted) and TITLE not in str(posted)


async def test_in_a_thread_the_button_message_goes_in_the_receipts_thread(world):
    _, item = await _item(world)
    ch, client = _channel(), _slack_client()

    await _handler(world.service, client)._deliver_item(item, ch, "T1", USER_ID)

    assert ch.send_file.await_args.kwargs["thread_id"] == "T1"
    assert ch.send_literal.await_args.args[1] == "T1"
    assert client.chat_postMessage.call_args.kwargs["thread_ts"] == "T1"
    ch.send_message.assert_not_awaited()


async def test_telegram_keeps_the_command_alone(world):
    created, item = await _item(world)
    ch, client = _channel("telegram"), _slack_client()

    await _handler(world.service, client)._deliver_item(item, ch, None, USER_ID)

    assert [c.args[0] for c in ch.send_message.await_args_list] == [f"`$task send {created.code}`"]
    client.chat_postMessage.assert_not_called()


async def test_without_the_click_path_wired_slack_gets_the_command_alone(world):
    created, item = await _item(world)
    ch = _channel()

    await _handler(world.service, None)._deliver_item(item, ch, None, USER_ID)

    assert [c.args[0] for c in ch.send_message.await_args_list] == [f"`$task send {created.code}`"]


async def test_a_failed_button_post_still_sends_the_command_alone(world):
    created, item = await _item(world)
    ch, client = _channel(), _slack_client()
    client.chat_postMessage.side_effect = RuntimeError("slack down")

    await _handler(world.service, client)._deliver_item(item, ch, "T1", USER_ID)

    assert [(c.args[0], c.args[1]) for c in ch.send_message.await_args_list] == [(f"`$task send {created.code}`", "T1")]


@pytest.mark.parametrize("failing", ["send_file", "send_literal"])
async def test_a_failed_receipt_posts_no_button(world, failing):
    _, item = await _item(world)
    ch, client = _channel(), _slack_client()
    getattr(ch, failing).side_effect = RuntimeError("slack down")

    await _handler(world.service, client)._deliver_item(item, ch, None, USER_ID)

    client.chat_postMessage.assert_not_called()
    ch.send_message.assert_not_awaited()


async def test_a_receipt_without_a_task_id_gets_the_command_alone_even_on_slack(world):
    created, item = await _item(world)
    item = DeliveryItem(type=EXTERNAL_TASK_RECEIPT_DELIVERY, data={**item.data, "task_id": None})
    ch, client = _channel(), _slack_client()

    await _handler(world.service, client)._deliver_item(item, ch, None, USER_ID)

    assert [c.args[0] for c in ch.send_message.await_args_list] == [f"`$task send {created.code}`"]
    client.chat_postMessage.assert_not_called()
