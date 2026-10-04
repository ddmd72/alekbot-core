"""
Unit tests for ConversationHandler's `skill_preview` delivery item.

Coverage target: `_deliver_item`'s SKILL_PREVIEW_DELIVERY branch — the skill draft is sent
verbatim as a file first (text posts truncate and reformat), then the save command alone as
the LAST message, so it is the easiest thing to copy and paste back.

Helpers `_make_handler`/`_make_channel` are copied from
tests/unit/handlers/test_conversation_handler_delivery.py per the task brief (no cross-test-file
imports).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.agent import AgentResponse, DeliveryItem
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.domain.skill import SKILL_PREVIEW_DELIVERY
from src.handlers.conversation_handler import ConversationHandler

_USER_ID = "user-test"
_ACCOUNT_ID = "acc-test"
_SESSION_ID = "sess-test"
_STATUS_MSG_ID = "msg-status-001"


def _make_context(text: str = "hello") -> MessageContext:
    return MessageContext(
        text=text,
        session_id=_SESSION_ID,
        user_id=_USER_ID,
        account_id=_ACCOUNT_ID,
        attachments=[],
        metadata={},
    )


def _make_channel(*, channel_id: str = "C-001") -> MagicMock:
    ch = MagicMock()
    ch.channel_id = channel_id
    ch.platform = "slack"
    ch.send_status_with_phrase = AsyncMock(return_value=(_STATUS_MSG_ID, "thinking..."))
    ch.send_status = AsyncMock()
    ch.send_message = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.update_message = AsyncMock()
    ch.send_rich_content = AsyncMock()
    ch.send_file = AsyncMock()
    ch.update_status_with_phrase_and_dots = AsyncMock()
    ch.get_status_phrase = AsyncMock(return_value="processing")
    ch.download_file = AsyncMock(return_value=None)
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


def _make_success(result) -> AgentResponse:
    return AgentResponse.success(
        task_id="task-1",
        agent_id=f"smart_response_agent_{_USER_ID}",
        result=result,
    )


def _make_handler(coordinator) -> ConversationHandler:
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(return_value=None)
    session_store.save_session = AsyncMock()

    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()
    agent_factory.get_session_store = MagicMock(return_value=session_store)
    agent_factory.invalidate_prompt_cache = MagicMock()
    user_repo = MagicMock()
    user_repo.get_user = AsyncMock(return_value=None)
    agent_factory.user_repo = user_repo

    return ConversationHandler(
        coordinator=coordinator,
        agent_factory=agent_factory,
        file_service=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
    )


def _simple_coordinator(result) -> MagicMock:
    coord = MagicMock()
    coord.route_message = AsyncMock(return_value=result)
    return coord


_SKILL_MD = '---\nname: my-skill\ndescription: "Does a thing"\n---\nBody text.\n'


def _make_item(command: str = "$skill save 7f3a") -> DeliveryItem:
    return DeliveryItem(
        type=SKILL_PREVIEW_DELIVERY,
        data={"name": "my-skill", "skill_md": _SKILL_MD, "command": command},
    )


class TestSkillPreviewDeliverItem:
    """`_deliver_item` dispatch for SKILL_PREVIEW_DELIVERY, called directly."""

    async def test_sends_file_verbatim_and_command_alone(self):
        handler = _make_handler(MagicMock())
        channel = _make_channel()
        item = _make_item()

        await handler._deliver_item(item, channel, thread_id="T1", user_id=_USER_ID)

        channel.send_file.assert_awaited_once_with(
            content=_SKILL_MD.encode("utf-8"),
            filename="my-skill.SKILL.md",
            title="Skill draft: my-skill",
            thread_id="T1",
        )
        channel.send_message.assert_awaited_once_with("`$skill save 7f3a`", "T1")

    async def test_file_sent_before_command_message(self):
        """send_file must be awaited before send_message — the command is the LAST message."""
        handler = _make_handler(MagicMock())
        channel = _make_channel()
        item = _make_item()

        parent = MagicMock()
        parent.attach_mock(channel.send_file, "send_file")
        parent.attach_mock(channel.send_message, "send_message")

        await handler._deliver_item(item, channel, thread_id=None, user_id=_USER_ID)

        call_names = [c[0] for c in parent.mock_calls]
        assert call_names.index("send_file") < call_names.index("send_message")


class TestSkillPreviewEndToEnd:
    """Driven via handle_message with a Smart response carrying the skill_preview item."""

    async def test_main_reply_then_file_then_command_in_order(self):
        item = _make_item()
        response = _make_success(SmartResponse(text="Here's the draft."))
        response.delivery_items.append(item)

        coord = _simple_coordinator(response)
        handler = _make_handler(coord)
        channel = _make_channel()

        parent = MagicMock()
        parent.attach_mock(channel.send_chunked_message, "send_chunked_message")
        parent.attach_mock(channel.send_file, "send_file")
        parent.attach_mock(channel.send_message, "send_message")

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        channel.send_file.assert_awaited_once_with(
            content=_SKILL_MD.encode("utf-8"),
            filename="my-skill.SKILL.md",
            title="Skill draft: my-skill",
            thread_id=None,
        )
        channel.send_message.assert_awaited_once_with("`$skill save 7f3a`", None)

        call_names = [c[0] for c in parent.mock_calls]
        assert (
            call_names.index("send_chunked_message")
            < call_names.index("send_file")
            < call_names.index("send_message")
        )
