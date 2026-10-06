"""
Unit tests for ConversationHandler's `skill_preview` delivery item — model-written files and
the change summary (Agent Skills delivery C, Task 7).

Extends `_deliver_item`'s SKILL_PREVIEW_DELIVERY branch: SKILL.md file → each model-written
file (flat uploaded name) → localized change-summary message (if any) → save command last.
A failed post (file or summary) sends the existing SKILL_PREVIEW_DELIVERY_FAILED notice and
suppresses the command, exactly like a failed SKILL.md post already does.

Helpers `_make_handler`/`_make_channel` are copied from
tests/unit/handlers/test_conversation_handler_skill_preview.py per that file's own comment
(no cross-test-file imports).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

from src.domain.agent import DeliveryItem
from src.domain.messaging import MessageContext
from src.domain.settings import ConsolidationSettings
from src.domain.skill import SKILL_PREVIEW_DELIVERY, Skill, SkillFile
from src.domain.ui_messages import UIMessage
from src.handlers.conversation_handler import ConversationHandler
from src.locales import en as en_locale
from src.locales import uk as uk_locale
from src.services.localization_service import LocalizationService
from src.adapters.file_localization_adapter import FileLocalizationAdapter

_USER_ID = "user-test"
_ACCOUNT_ID = "acc-test"
_SESSION_ID = "sess-test"
_STATUS_MSG_ID = "msg-status-001"


def _ui_uk(message: UIMessage, **fmt) -> str:
    template = uk_locale.UI_STRINGS[message.value]
    return template.format(**fmt) if fmt else template


def _ui_en(message: UIMessage, **fmt) -> str:
    template = en_locale.UI_STRINGS[message.value]
    return template.format(**fmt) if fmt else template


def _make_context(text: str = "hello") -> MessageContext:
    return MessageContext(
        text=text,
        session_id=_SESSION_ID,
        user_id=_USER_ID,
        account_id=_ACCOUNT_ID,
        attachments=[],
        metadata={},
    )


def _make_channel(*, channel_id: str = "C-001", language: str | None = None) -> MagicMock:
    ch = MagicMock()
    ch.channel_id = channel_id
    ch.platform = "slack"
    ch.language = language
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


def _make_handler(coordinator=None, *, localized: bool = False) -> ConversationHandler:
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

    localization = LocalizationService(FileLocalizationAdapter()) if localized else None

    return ConversationHandler(
        coordinator=coordinator or MagicMock(),
        agent_factory=agent_factory,
        file_service=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
        localization=localization,
    )


def _make_skill_list_handler(skill_service) -> ConversationHandler:
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    agent_factory = MagicMock()
    agent_factory.get_session_store = MagicMock(return_value=session_store)

    return ConversationHandler(
        coordinator=MagicMock(),
        agent_factory=agent_factory,
        file_service=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
        skill_service=skill_service,
    )


def _record_calls(channel: MagicMock) -> list:
    """Wire send_file/send_message to append (kind, label) tuples to a shared sequence,
    in call order, while still behaving like an AsyncMock (awaitable, inspectable)."""
    seq: list = []

    async def fake_send_file(content, filename, title, thread_id):
        seq.append(("file", filename))

    async def fake_send_message(text, thread_id):
        seq.append(("message", text))

    channel.send_file = AsyncMock(side_effect=fake_send_file)
    channel.send_message = AsyncMock(side_effect=fake_send_message)
    return seq


_SKILL_MD = '---\nname: fs\ndescription: "Does a thing"\n---\nBody text.\n'


def _make_item(**overrides) -> DeliveryItem:
    data = {
        "name": "fs",
        "skill_md": _SKILL_MD,
        "command": "$skill save ab12",
    }
    data.update(overrides)
    return DeliveryItem(type=SKILL_PREVIEW_DELIVERY, data=data)


class TestSkillPreviewFilesAndSummary:
    async def test_order_skill_md_files_summary_command(self):
        handler = _make_handler(localized=True)
        channel = _make_channel(language="en")
        seq = _record_calls(channel)
        item = _make_item(
            files=[
                {"path": "references/a.md", "content": "AAA"},
                {"path": "b.csv", "content": "x,y"},
            ],
            summary=[
                {"kind": "new", "path": "references/a.md", "size": 3, "source": None, "count": 0},
                {"kind": "new", "path": "b.csv", "size": 3, "source": "rates.csv", "count": 0},
            ],
        )

        await handler._deliver_item(item, channel, thread_id="T1", user_id=_USER_ID)

        assert seq == [
            ("file", "fs.SKILL.md"),
            ("file", "fs.references__a.md"),
            ("file", "fs.b.csv"),
            (
                "message",
                '+ references/a.md (new, 3 B)\n+ b.csv ← "rates.csv" (your upload, 3 B)',
            ),
            ("message", "`$skill save ab12`"),
        ]

        # File bytes are verbatim UTF-8 of the content.
        calls = channel.send_file.call_args_list
        assert calls[0].kwargs["content"] == _SKILL_MD.encode("utf-8")
        assert calls[1].kwargs["content"] == b"AAA"
        assert calls[2].kwargs["content"] == b"x,y"
        assert calls[1].kwargs["thread_id"] == "T1"
        assert calls[1].kwargs["title"] == _ui_en(
            UIMessage.SKILL_PREVIEW_REF_FILE_TITLE, name="fs", path="references/a.md",
        )

    async def test_any_file_failure_suppresses_command(self):
        handler = _make_handler()
        channel = _make_channel()
        item = _make_item(
            files=[
                {"path": "references/a.md", "content": "AAA"},
                {"path": "b.csv", "content": "x,y"},
            ],
        )
        # Second send_file (the first ref file) fails.
        channel.send_file = AsyncMock(side_effect=[None, RuntimeError("upload failed")])

        await handler._deliver_item(item, channel, thread_id="T1", user_id=_USER_ID)

        channel.send_message.assert_awaited_once_with(_ui_uk(UIMessage.SKILL_PREVIEW_DELIVERY_FAILED), "T1")
        for call_args in channel.send_message.call_args_list:
            assert "$skill save" not in call_args.args[0]

    async def test_summary_failure_suppresses_command(self):
        handler = _make_handler()
        channel = _make_channel()
        item = _make_item(
            summary=[{"kind": "unchanged", "path": "", "size": 0, "source": None, "count": 2}],
        )
        # The summary post (first send_message call) raises; the failure notice (second
        # call) succeeds — the command must never be attempted after that.
        channel.send_message = AsyncMock(side_effect=[RuntimeError("summary send failed"), None])

        await handler._deliver_item(item, channel, thread_id="T1", user_id=_USER_ID)

        calls = channel.send_message.call_args_list
        assert calls[-1] == call(_ui_uk(UIMessage.SKILL_PREVIEW_DELIVERY_FAILED), "T1")
        for call_args in calls:
            assert "$skill save" not in call_args.args[0]

    async def test_no_files_no_summary_behaves_as_before(self):
        handler = _make_handler()
        channel = _make_channel()
        item = _make_item()  # no "files"/"summary" keys at all

        await handler._deliver_item(item, channel, thread_id="T1", user_id=_USER_ID)

        channel.send_file.assert_awaited_once_with(
            content=_SKILL_MD.encode("utf-8"),
            filename="fs.SKILL.md",
            title=_ui_uk(UIMessage.SKILL_PREVIEW_FILE_TITLE, name="fs"),
            thread_id="T1",
        )
        channel.send_message.assert_awaited_once_with("`$skill save ab12`", "T1")

    async def test_empty_files_and_summary_lists_behave_as_before(self):
        """Explicit empty lists (not missing keys) must not post an empty summary message
        or any extra file posts."""
        handler = _make_handler()
        channel = _make_channel()
        item = _make_item(files=[], summary=[])

        await handler._deliver_item(item, channel, thread_id="T1", user_id=_USER_ID)

        channel.send_file.assert_awaited_once()
        channel.send_message.assert_awaited_once_with("`$skill save ab12`", "T1")


class TestSkillListFileCount:
    async def test_skill_list_shows_file_count(self):
        service = MagicMock()
        service.list_owned = AsyncMock()
        custom = [
            Skill(
                name="fs",
                description="Use when x.",
                body="body",
                version=1,
                files=[
                    SkillFile(path="a.md", sha256="0" * 64, size=3),
                    SkillFile(path="b.csv", sha256="1" * 64, size=3),
                ],
            ),
        ]
        service.list_owned.return_value = (custom, [])
        handler = _make_skill_list_handler(service)
        channel = _make_channel()
        channel.send_message = AsyncMock()

        await handler.handle_command("skill list", _make_context(), channel)

        text = channel.send_message.call_args.args[0]
        assert f"- fs — Use when x. ({_ui_uk(UIMessage.SKILL_LIST_FILES, count=2)})" in text

    async def test_skill_list_no_files_unchanged(self):
        service = MagicMock()
        service.list_owned = AsyncMock()
        custom = [Skill(name="fs", description="Use when x.", body="body", version=1)]
        service.list_owned.return_value = (custom, [])
        handler = _make_skill_list_handler(service)
        channel = _make_channel()
        channel.send_message = AsyncMock()

        await handler.handle_command("skill list", _make_context(), channel)

        text = channel.send_message.call_args.args[0]
        assert "- fs — Use when x." in text
        assert "files" not in text.lower() and "файл" not in text.lower()

    async def test_builtin_skills_show_no_file_details(self):
        """Owner ruling: no details about system skills in `$skill list`."""
        service = MagicMock()
        service.list_owned = AsyncMock()
        system = [Skill(name="skill-creator", description="Builds skills", body="y", version=0)]
        service.list_owned.return_value = ([], system)
        handler = _make_skill_list_handler(service)
        channel = _make_channel()
        channel.send_message = AsyncMock()

        await handler.handle_command("skill list", _make_context(), channel)

        text = channel.send_message.call_args.args[0]
        assert "- skill-creator" in text
        assert "files" not in text.lower() and "файл" not in text.lower()
