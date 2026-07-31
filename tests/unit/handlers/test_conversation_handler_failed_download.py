"""ConversationHandler: what the agent sees when an attachment fails to download.

Before 2026-07-31 a failed `download_file` produced a log warning and nothing else — the
attachment slot was simply absent, so the agent could not tell "no file was sent" from "the
file did not arrive" and answered from the text alone. The observed incident: an over-long
filename raised errno 36, the file vanished, and only the model's own guesswork hinted at it.

The replacement is a system note in the attachment's place — the same contract the size and
conversion alerts already use. Deliberately NOT a Quick fallback: the primary agent has not
failed, one of its inputs has, and downgrading a working request to the emergency agent would
cost delegation and answer quality for a request that is still perfectly answerable.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentResponse
from src.domain.messaging import FileAttachment, MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler

_USER_ID = "user-test"


def _make_context(text="Вот что пишет почта про мою посылку", attachments=None) -> MessageContext:
    return MessageContext(
        text=text,
        session_id="sess-test",
        user_id=_USER_ID,
        account_id="acc-test",
        attachments=attachments or [],
        metadata={},
    )


def _make_channel(download_result=None) -> MagicMock:
    ch = MagicMock()
    ch.channel_id = "C-001"
    ch.platform = "slack"
    ch.send_status_with_phrase = AsyncMock(return_value=("msg-1", "thinking..."))
    ch.send_status = AsyncMock()
    ch.send_message = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.update_message = AsyncMock()
    ch.send_rich_content = AsyncMock()
    ch.update_status_with_phrase_and_dots = AsyncMock()
    ch.get_status_phrase = AsyncMock(return_value="processing")
    ch.download_file = AsyncMock(return_value=download_result)
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


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


def _coordinator() -> MagicMock:
    coord = MagicMock()
    coord.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="task-1",
        agent_id=f"smart_response_agent_{_USER_ID}",
        result=SmartResponse(text="OK"),
    ))
    return coord


def _parts_seen_by_agent(coord) -> list:
    message = coord.route_message.await_args.args[0]
    return message.context["current_message_parts"]


ATTACHMENT = FileAttachment(
    url="https://files.slack.com/files-pri/T1-F1/" + "x" * 260 + ".html",
    mime_type="text/html",
    filename="view-source_https___www.correosaduanas.es_" + "x" * 200 + ".html",
)


class TestFailedDownloadReachesTheAgent:

    async def test_a_system_note_takes_the_attachment_slot(self):
        coord = _coordinator()
        handler = _make_handler(coord)

        await handler.handle_message(_make_context(attachments=[ATTACHMENT]), _make_channel())

        notes = [p.text for p in _parts_seen_by_agent(coord) if p.text and "[System:" in p.text]
        assert len(notes) == 1, "the lost attachment must leave exactly one note"
        assert "could not be retrieved" in notes[0]

    async def test_the_note_names_the_file(self):
        coord = _coordinator()
        handler = _make_handler(coord)

        await handler.handle_message(_make_context(attachments=[ATTACHMENT]), _make_channel())

        note = next(p.text for p in _parts_seen_by_agent(coord) if p.text and "[System:" in p.text)
        assert ATTACHMENT.filename in note

    async def test_the_user_text_still_reaches_the_agent(self):
        """The request stays answerable — the note supplements the text, it does not replace it."""
        coord = _coordinator()
        handler = _make_handler(coord)
        ctx = _make_context(attachments=[ATTACHMENT])

        await handler.handle_message(ctx, _make_channel())

        texts = [p.text for p in _parts_seen_by_agent(coord) if p.text]
        assert ctx.text in texts

    async def test_the_agent_is_told_not_to_invent_the_content(self):
        coord = _coordinator()
        handler = _make_handler(coord)

        await handler.handle_message(_make_context(attachments=[ATTACHMENT]), _make_channel())

        note = next(p.text for p in _parts_seen_by_agent(coord) if p.text and "[System:" in p.text)
        assert "Do not guess" in note
        assert "send it again" in note

    async def test_one_note_per_lost_attachment(self):
        coord = _coordinator()
        handler = _make_handler(coord)
        second = FileAttachment(url="https://x/y.pdf", mime_type="application/pdf", filename="y.pdf")

        await handler.handle_message(
            _make_context(attachments=[ATTACHMENT, second]), _make_channel()
        )

        notes = [p.text for p in _parts_seen_by_agent(coord) if p.text and "[System:" in p.text]
        assert len(notes) == 2
        assert any("y.pdf" in n for n in notes)

    async def test_the_note_survives_into_history(self):
        """A later turn must still show that a file was attempted and lost."""
        coord = _coordinator()
        handler = _make_handler(coord)

        await handler.handle_message(_make_context(attachments=[ATTACHMENT]), _make_channel())

        batch = handler.agent_factory.get_session_store().append_messages_batch
        _session_id, messages = batch.await_args.args[0], batch.await_args.args[1]
        user_parts = next(m.parts for m in messages if m.role == "user")
        assert any(p.text and "could not be retrieved" in p.text for p in user_parts)


class TestSuccessfulDownloadIsUnaffected:

    async def test_no_note_when_the_file_arrives(self, tmp_path):
        downloaded = tmp_path / "ok.txt"
        downloaded.write_text("content")
        coord = _coordinator()
        handler = _make_handler(coord)

        await handler.handle_message(
            _make_context(attachments=[FileAttachment(
                url="https://x/ok.txt", mime_type="text/plain", filename="ok.txt",
            )]),
            _make_channel(download_result=str(downloaded)),
        )

        notes = [p.text for p in _parts_seen_by_agent(coord)
                 if p.text and "could not be retrieved" in p.text]
        assert notes == []

    async def test_attachment_without_url_is_skipped_silently(self):
        """No URL means nothing was ever sent to us — not a lost file."""
        coord = _coordinator()
        handler = _make_handler(coord)

        await handler.handle_message(
            _make_context(attachments=[FileAttachment(
                url="", mime_type="text/plain", filename="ghost.txt",
            )]),
            _make_channel(),
        )

        notes = [p.text for p in _parts_seen_by_agent(coord)
                 if p.text and "could not be retrieved" in p.text]
        assert notes == []
