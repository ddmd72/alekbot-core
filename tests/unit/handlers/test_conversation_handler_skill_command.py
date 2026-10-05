"""
Unit tests for ConversationHandler's `$skill` command family
(`_handle_skill_command` → save / list / delete, dispatched from `handle_command`).

Helpers `_make_handler`/`_make_channel` are copied from
tests/unit/handlers/test_conversation_handler_skill_preview.py per that file's own
comment (no cross-test-file imports).

Covers: no skill_service configured, bare `$skill` / unknown sub-command usage, each
sub-command's happy path and typed-exception paths, the history pair appended once on a
successful save (and never on a failed one), a history-append failure being logged but
not raised, and an unexpected (untyped) exception from SkillService never escaping
`handle_command` (controller requirement from the Task 4 review).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.exceptions import (
    SkillCapExceeded,
    SkillDraftNotFound,
    SkillNameReserved,
    SkillRejected,
)
from src.domain.messaging import MessageContext
from src.domain.settings import ConsolidationSettings
from src.domain.skill import Skill
from src.domain.ui_messages import UIMessage
from src.handlers.conversation_handler import ConversationHandler
from src.locales import uk as uk_locale

_USER_ID = "user-test"
_ACCOUNT_ID = "acc-test"
_SESSION_ID = "sess-test"


def _ui(message: UIMessage, **fmt) -> str:
    """Expected localized string — handler falls back to the uk locale when no
    LocalizationService is wired (same default `_ui_string` itself documents)."""
    template = uk_locale.UI_STRINGS[message.value]
    return template.format(**fmt) if fmt else template


def _make_context(text: str = "skill list") -> MessageContext:
    return MessageContext(
        text=text,
        session_id=_SESSION_ID,
        user_id=_USER_ID,
        account_id=_ACCOUNT_ID,
        attachments=[],
        metadata={},
    )


def _make_channel() -> MagicMock:
    ch = MagicMock()
    ch.send_message = AsyncMock()
    return ch


def _make_handler(skill_service=None) -> ConversationHandler:
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()

    agent_factory = MagicMock()
    agent_factory.get_session_store = MagicMock(return_value=session_store)

    coordinator = MagicMock()

    return ConversationHandler(
        coordinator=coordinator,
        agent_factory=agent_factory,
        file_service=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
        skill_service=skill_service,
    )


def _make_skill_service() -> MagicMock:
    service = MagicMock()
    service.save_draft = AsyncMock()
    service.list_owned = AsyncMock()
    service.delete = AsyncMock()
    return service


# ---------------------------------------------------------------------------
# No service / usage
# ---------------------------------------------------------------------------

class TestSkillCommandAvailability:
    async def test_no_skill_service_configured_replies_unavailable(self):
        handler = _make_handler(skill_service=None)
        channel = _make_channel()

        await handler.handle_command("skill list", _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_UNAVAILABLE), thread_id=None,
        )

    async def test_bare_skill_prints_usage(self):
        service = _make_skill_service()
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill", _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_USAGE), thread_id=None,
        )
        service.list_owned.assert_not_called()

    async def test_unknown_subcommand_prints_usage(self):
        service = _make_skill_service()
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill rename foo", _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_USAGE), thread_id=None,
        )

    @pytest.mark.parametrize("command", ["skill save", "skill save a b", "skill delete"])
    async def test_wrong_arg_count_prints_usage(self, command):
        service = _make_skill_service()
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command(command, _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_USAGE), thread_id=None,
        )
        service.save_draft.assert_not_called()
        service.delete.assert_not_called()


# ---------------------------------------------------------------------------
# $skill save CODE
# ---------------------------------------------------------------------------

class TestSkillSave:
    async def test_success_replies_saved_and_appends_history_once(self):
        service = _make_skill_service()
        service.save_draft.return_value = ("my-skill", 1)
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill save 7f3a", _make_context(), channel)

        service.save_draft.assert_awaited_once_with(_USER_ID, _ACCOUNT_ID, "7f3a")
        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_SAVED, name="my-skill", version=1), thread_id=None,
        )
        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()
        _, kwargs = session_store.append_messages_batch.call_args
        assert kwargs["session_id"] == _SESSION_ID
        assert kwargs["owner_id"] == _USER_ID
        messages = kwargs["messages"]
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert 'skill "my-skill" v1 saved' in messages[0].parts[0].text
        assert messages[1].role == "model"
        assert messages[1].parts[0].text == "Saved skill my-skill v1."
        assert messages[1].parts[0].full_text == "Saved skill my-skill v1."

    async def test_draft_not_found_replies_and_does_not_append_history(self):
        service = _make_skill_service()
        service.save_draft.side_effect = SkillDraftNotFound("no pending draft")
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill save dead1", _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_DRAFT_NOT_FOUND, code="dead1"), thread_id=None,
        )
        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_not_called()

    @pytest.mark.parametrize(
        "exc", [
            SkillRejected("body flagged by the security check"),
            SkillNameReserved("'foo' is a system skill name"),
            SkillCapExceeded("user already has 20 skills (cap 20)"),
        ],
    )
    async def test_rejection_exceptions_reply_not_saved_with_reason(self, exc):
        service = _make_skill_service()
        service.save_draft.side_effect = exc
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill save abcd", _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_NOT_SAVED, reason=str(exc)), thread_id=None,
        )
        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_not_called()

    async def test_history_append_failure_is_logged_not_raised(self):
        service = _make_skill_service()
        service.save_draft.return_value = ("my-skill", 2)
        handler = _make_handler(skill_service=service)
        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.side_effect = RuntimeError("firestore down")
        channel = _make_channel()

        # Must not raise — the skill is already saved, only history bookkeeping failed.
        await handler.handle_command("skill save 7f3a", _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_SAVED, name="my-skill", version=2), thread_id=None,
        )


# ---------------------------------------------------------------------------
# $skill list
# ---------------------------------------------------------------------------

class TestSkillList:
    async def test_lists_custom_and_system_skills(self):
        service = _make_skill_service()
        custom = [Skill(name="my-skill", description="Does a thing", body="x", version=1)]
        system = [Skill(name="skill-creator", description="Builds skills", body="y", version=0)]
        service.list_owned.return_value = (custom, system)
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill list", _make_context(), channel)

        text = channel.send_message.call_args.args[0]
        assert _ui(UIMessage.SKILL_LIST_HEADER) in text
        assert "- my-skill — Does a thing" in text
        assert _ui(UIMessage.SKILL_SYSTEM_HEADER) in text
        assert "- skill-creator" in text

    async def test_empty_custom_list_shows_empty_message(self):
        service = _make_skill_service()
        service.list_owned.return_value = ([], [])
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill list", _make_context(), channel)

        text = channel.send_message.call_args.args[0]
        assert _ui(UIMessage.SKILL_LIST_EMPTY) in text

    async def test_system_skill_shadowed_by_custom_copy_not_listed_under_built_in(self):
        """A custom skill with the same name as a system one shadows it (SkillService
        merges them this way for `use_skill`) — it must appear once, under the owner's
        own skills, not a second time under Built-in."""
        service = _make_skill_service()
        custom = [Skill(name="skill-creator", description="My own version", body="x", version=1)]
        system = [
            Skill(name="skill-creator", description="Builds skills", body="y", version=0),
            Skill(name="domain-competency-research", description="Maps a domain", body="z", version=0),
        ]
        service.list_owned.return_value = (custom, system)
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill list", _make_context(), channel)

        text = channel.send_message.call_args.args[0]
        assert "- skill-creator — My own version" in text
        assert "- domain-competency-research" in text
        # Only the custom line for "skill-creator" — not a second, Built-in one.
        assert text.count("skill-creator") == 1


# ---------------------------------------------------------------------------
# $skill delete NAME
# ---------------------------------------------------------------------------

class TestSkillDelete:
    async def test_deleted_true_replies_deleted(self):
        service = _make_skill_service()
        service.delete.return_value = True
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill delete my-skill", _make_context(), channel)

        service.delete.assert_awaited_once_with(_USER_ID, "my-skill")
        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_DELETED, name="my-skill"), thread_id=None,
        )

    async def test_deleted_false_replies_not_found(self):
        service = _make_skill_service()
        service.delete.return_value = False
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill delete ghost", _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_NOT_FOUND, name="ghost"), thread_id=None,
        )

    async def test_system_skill_name_replies_built_in(self):
        service = _make_skill_service()
        service.delete.side_effect = SkillNameReserved("'skill-creator' is a system skill")
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill delete skill-creator", _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_BUILT_IN, name="skill-creator"), thread_id=None,
        )


# ---------------------------------------------------------------------------
# Unexpected (untyped) errors — controller requirement from the Task 4 review
# ---------------------------------------------------------------------------

class TestSkillCommandUnexpectedErrors:
    async def test_unexpected_exception_from_list_replies_unavailable_no_raise(self, caplog):
        service = _make_skill_service()
        service.list_owned.side_effect = RuntimeError("firestore unavailable")
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        # Must not raise.
        await handler.handle_command("skill list", _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_UNAVAILABLE), thread_id=None,
        )

    async def test_unexpected_exception_from_delete_replies_unavailable_no_raise(self):
        service = _make_skill_service()
        service.delete.side_effect = ValueError("unexpected")
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill delete foo", _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_UNAVAILABLE), thread_id=None,
        )

    async def test_unexpected_exception_from_save_replies_unavailable_no_raise(self):
        service = _make_skill_service()
        service.save_draft.side_effect = KeyError("boom")
        handler = _make_handler(skill_service=service)
        channel = _make_channel()

        await handler.handle_command("skill save 7f3a", _make_context(), channel)

        channel.send_message.assert_awaited_once_with(
            _ui(UIMessage.SKILL_UNAVAILABLE), thread_id=None,
        )
