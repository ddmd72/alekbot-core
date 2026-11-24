"""`InteractiveMessagePort.post_reply`: an interactive message posted in the thread of a message, so the Send button of a
held external task lands next to the receipt it belongs to. Slack posts it as a thread reply (mocked at the slack_sdk
client boundary only); the router hands it to the platform's adapter; a platform without replies refuses."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from src.adapters.notification_channel_factory import NotificationChannelFactory
from src.adapters.plain_text_interactive_message_adapter import PlainTextInteractiveMessageAdapter
from src.adapters.slack.interactive_message_adapter import SlackInteractiveMessageAdapter
from src.domain.external_task_receipt import send_button_message
from src.domain.messaging import MessageRef
from src.infrastructure.interactive_message_router import InteractiveMessageRouter

MESSAGE = send_button_message("tsk-1", "a1b2c3d4", "Send")
THREAD = "1761295900.000200"


def client():
    c = MagicMock()
    c.chat_postMessage = AsyncMock(return_value={"ok": True, "channel": "D0REAL", "ts": "1760.01"})
    return c


async def test_slack_posts_the_same_blocks_as_post_in_the_thread_of_the_message():
    root, reply = client(), client()
    await SlackInteractiveMessageAdapter(root).post("slack", "D0OWNER", MESSAGE)

    ref = await SlackInteractiveMessageAdapter(reply).post_reply("slack", "D0OWNER", MESSAGE, THREAD)

    assert ref == MessageRef("slack", "D0REAL", "1760.01")
    posted = reply.chat_postMessage.call_args.kwargs
    assert posted["thread_ts"] == THREAD and posted["channel"] == "D0OWNER"
    assert {k: v for k, v in posted.items() if k != "thread_ts"} == root.chat_postMessage.call_args.kwargs
    assert "thread_ts" not in root.chat_postMessage.call_args.kwargs


async def test_the_send_buttons_message_is_one_button_with_the_code_and_the_task_binding():
    c = client()

    await SlackInteractiveMessageAdapter(c).post_reply("slack", "D0OWNER", MESSAGE, THREAD)

    prompt, actions = c.chat_postMessage.call_args.kwargs["blocks"]
    assert prompt == {"type": "section", "text": {"type": "mrkdwn", "text": "`$task send a1b2c3d4`"}}
    assert actions["block_id"] == "external_task_send:tsk-1"
    [button] = actions["elements"]
    assert (button["action_id"], button["value"], button["text"]["text"]) == ("a1b2c3d4", "a1b2c3d4", "Send")


async def test_slack_refuses_another_platform_and_raises_its_own_error():
    c = client()
    with pytest.raises(ValueError):
        await SlackInteractiveMessageAdapter(c).post_reply("telegram", "555", MESSAGE, THREAD)
    c.chat_postMessage.assert_not_called()
    c.chat_postMessage = AsyncMock(side_effect=SlackApiError("channel_not_found", {"ok": False}))
    with pytest.raises(SlackApiError):
        await SlackInteractiveMessageAdapter(c).post_reply("slack", "D0OWNER", MESSAGE, THREAD)


async def test_the_router_hands_a_reply_to_the_adapter_of_its_platform():
    c = client()
    router = InteractiveMessageRouter({"slack": SlackInteractiveMessageAdapter(c)})

    ref = await router.post_reply("slack", "D0OWNER", MESSAGE, THREAD)

    assert ref == MessageRef("slack", "D0REAL", "1760.01")
    assert c.chat_postMessage.call_args.kwargs["thread_ts"] == THREAD
    with pytest.raises(ValueError, match="whatsapp"):
        await router.post_reply("whatsapp", "1", MESSAGE, THREAD)


async def test_a_platform_without_replies_refuses():
    adapter = PlainTextInteractiveMessageAdapter(NotificationChannelFactory({}))
    with pytest.raises(NotImplementedError):
        await adapter.post_reply("telegram", "555", MESSAGE, THREAD)
