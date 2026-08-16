"""ConversationHandler: a voice message is speech, not a document.

Down the ordinary file path a transcript becomes a reference-only MessagePart, which never
reaches session history — so a spoken turn would leave consolidation nothing to read, and the
history slot would be filled by the synthetic "look at this file" fallback instead. The voice
branch transcribes before the file path and makes the transcript the user's own turn.

Without an audio service nothing changes: the attachment stays a file and the existing
"transcription unavailable" alert still fires.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentResponse
from src.domain.messaging import FileAttachment, MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler
from src.ports.audio_transcription_port import AudioTranscriptionPort

_USER_ID = "user-test"

VOICE = FileAttachment(
    url="https://files.slack.com/files-pri/T1-F1/audio_message.m4a",
    mime_type="audio/mp4",
    filename="audio_message.m4a",
    is_voice_message=True,
)


def _make_context(text="", attachments=None) -> MessageContext:
    return MessageContext(
        text=text,
        session_id="sess-test",
        user_id=_USER_ID,
        account_id="acc-test",
        attachments=attachments or [],
        metadata={},
    )


def _make_channel(download_result="/tmp/audio_message.m4a") -> MagicMock:
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


def _coordinator() -> MagicMock:
    coord = MagicMock()
    coord.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="task-1",
        agent_id=f"smart_response_agent_{_USER_ID}",
        result=SmartResponse(text="OK"),
    ))
    return coord


def _make_handler(coordinator, audio_service=None) -> ConversationHandler:
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
        audio_service=audio_service,
    )


def _transcriber(text="напомни завтра позвонить в банк") -> MagicMock:
    svc = AsyncMock(spec=AudioTranscriptionPort)
    svc.transcribe = AsyncMock(return_value=text)
    return svc


def _parts_seen_by_agent(coord) -> list:
    message = coord.route_message.await_args.args[0]
    return message.context["current_message_parts"]


class TestTranscriptBecomesTheUserTurn:

    async def test_transcript_reaches_the_agent_as_text(self):
        coord = _coordinator()
        handler = _make_handler(coord, audio_service=_transcriber())

        await handler.handle_message(_make_context(attachments=[VOICE.__class__(**vars(VOICE))]),
                                     _make_channel())

        texts = [p.text for p in _parts_seen_by_agent(coord) if p.text]
        assert "напомни завтра позвонить в банк" in texts

    async def test_transcript_survives_into_history(self):
        """The whole point: a spoken turn must be there for consolidation to read."""
        coord = _coordinator()
        handler = _make_handler(coord, audio_service=_transcriber())

        await handler.handle_message(_make_context(attachments=[VOICE.__class__(**vars(VOICE))]),
                                     _make_channel())

        batch = handler.agent_factory.get_session_store().append_messages_batch
        messages = batch.await_args.args[1]
        user_parts = next(m.parts for m in messages if m.role == "user")
        assert any(p.text and "позвонить в банк" in p.text for p in user_parts)

    async def test_synthetic_file_fallback_does_not_fire(self):
        """'Подивись на цей файл' must not take the history slot from actual speech."""
        coord = _coordinator()
        handler = _make_handler(coord, audio_service=_transcriber())
        ctx = _make_context(attachments=[VOICE.__class__(**vars(VOICE))])

        await handler.handle_message(ctx, _make_channel())

        assert ctx.text == "напомни завтра позвонить в банк"

    async def test_voice_attachment_does_not_go_down_the_file_path(self):
        """One download, one transcription — not a GCS upload plus a re-read."""
        coord = _coordinator()
        handler = _make_handler(coord, audio_service=_transcriber())
        ctx = _make_context(attachments=[VOICE.__class__(**vars(VOICE))])

        await handler.handle_message(ctx, _make_channel())

        assert ctx.attachments == []

    async def test_caption_is_kept_and_transcript_appended(self):
        coord = _coordinator()
        handler = _make_handler(coord, audio_service=_transcriber())
        ctx = _make_context(text="контекст", attachments=[VOICE.__class__(**vars(VOICE))])

        await handler.handle_message(ctx, _make_channel())

        assert ctx.text.startswith("контекст")
        assert "напомни завтра позвонить в банк" in ctx.text

    async def test_transcriber_receives_path_and_mime(self):
        coord = _coordinator()
        svc = _transcriber()
        handler = _make_handler(coord, audio_service=svc)

        await handler.handle_message(_make_context(attachments=[VOICE.__class__(**vars(VOICE))]),
                                     _make_channel(download_result="/tmp/x.m4a"))

        svc.transcribe.assert_awaited_once_with("/tmp/x.m4a", "audio/mp4", languages=None)


class TestSpokenLanguagesComeFromUserSettings:
    """A multilingual household is the normal case, so the codes are a user setting —
    not the UI language, whose closed enum cannot even express "ru"."""

    def _handler_with_config(self, coord, svc, voice_languages):
        handler = _make_handler(coord, audio_service=svc)
        profile = MagicMock()
        profile.config = MagicMock(voice_languages=voice_languages)
        handler.agent_factory.user_repo.get_user = AsyncMock(return_value=profile)
        return handler

    async def test_configured_languages_reach_the_transcriber(self):
        coord = _coordinator()
        svc = _transcriber()
        handler = self._handler_with_config(coord, svc, ["ru", "uk", "en", "fr", "es"])

        await handler.handle_message(_make_context(attachments=[VOICE.__class__(**vars(VOICE))]),
                                     _make_channel(download_result="/tmp/x.m4a"))

        svc.transcribe.assert_awaited_once_with(
            "/tmp/x.m4a", "audio/mp4", languages=["ru", "uk", "en", "fr", "es"],
        )

    async def test_unset_languages_mean_auto_detect(self):
        coord = _coordinator()
        svc = _transcriber()
        handler = self._handler_with_config(coord, svc, None)

        await handler.handle_message(_make_context(attachments=[VOICE.__class__(**vars(VOICE))]),
                                     _make_channel(download_result="/tmp/x.m4a"))

        assert svc.transcribe.await_args.kwargs["languages"] is None

    async def test_empty_list_means_auto_detect(self):
        coord = _coordinator()
        svc = _transcriber()
        handler = self._handler_with_config(coord, svc, [])

        await handler.handle_message(_make_context(attachments=[VOICE.__class__(**vars(VOICE))]),
                                     _make_channel(download_result="/tmp/x.m4a"))

        assert svc.transcribe.await_args.kwargs["languages"] is None

    async def test_profile_lookup_failure_does_not_break_transcription(self):
        coord = _coordinator()
        svc = _transcriber()
        handler = _make_handler(coord, audio_service=svc)
        handler.agent_factory.user_repo.get_user = AsyncMock(side_effect=RuntimeError("firestore"))

        await handler.handle_message(_make_context(attachments=[VOICE.__class__(**vars(VOICE))]),
                                     _make_channel(download_result="/tmp/x.m4a"))

        assert svc.transcribe.await_args.kwargs["languages"] is None


class TestVoiceFailuresAreVisible:

    async def test_failed_transcription_leaves_a_system_note(self):
        coord = _coordinator()
        svc = _transcriber()
        svc.transcribe = AsyncMock(side_effect=RuntimeError("provider down"))
        handler = _make_handler(coord, audio_service=svc)

        await handler.handle_message(_make_context(attachments=[VOICE.__class__(**vars(VOICE))]),
                                     _make_channel())

        notes = [p.text for p in _parts_seen_by_agent(coord) if p.text and "[System:" in p.text]
        assert len(notes) == 1
        assert "could not be transcribed" in notes[0]
        assert "Do not guess" in notes[0]

    async def test_failed_voice_still_gives_the_router_something_to_classify(self):
        """The attachment has already left the file path, so if the note is not the turn the
        Router gets an empty query and returns CANNOT_HANDLE (observed live 2026-08-16)."""
        coord = _coordinator()
        svc = _transcriber()
        svc.transcribe = AsyncMock(side_effect=RuntimeError("provider down"))
        handler = _make_handler(coord, audio_service=svc)
        ctx = _make_context(attachments=[VOICE.__class__(**vars(VOICE))])

        await handler.handle_message(ctx, _make_channel())

        assert "could not be transcribed" in ctx.text
        assert coord.route_message.await_args.args[0].payload["text"]

    async def test_caption_survives_a_failed_voice(self):
        coord = _coordinator()
        svc = _transcriber()
        svc.transcribe = AsyncMock(side_effect=RuntimeError("provider down"))
        handler = _make_handler(coord, audio_service=svc)
        ctx = _make_context(text="контекст", attachments=[VOICE.__class__(**vars(VOICE))])

        await handler.handle_message(ctx, _make_channel())

        assert ctx.text == "контекст"

    async def test_empty_transcription_leaves_a_system_note(self):
        coord = _coordinator()
        handler = _make_handler(coord, audio_service=_transcriber(text="   "))

        await handler.handle_message(_make_context(attachments=[VOICE.__class__(**vars(VOICE))]),
                                     _make_channel())

        notes = [p.text for p in _parts_seen_by_agent(coord) if p.text and "[System:" in p.text]
        assert any("could not be transcribed" in n for n in notes)

    async def test_failed_download_leaves_the_download_note(self):
        coord = _coordinator()
        handler = _make_handler(coord, audio_service=_transcriber())

        await handler.handle_message(_make_context(attachments=[VOICE.__class__(**vars(VOICE))]),
                                     _make_channel(download_result=None))

        notes = [p.text for p in _parts_seen_by_agent(coord) if p.text and "[System:" in p.text]
        assert any("could not be retrieved" in n for n in notes)


class TestWithoutAudioServiceNothingChanges:

    async def test_voice_stays_an_attachment(self):
        coord = _coordinator()
        handler = _make_handler(coord, audio_service=None)
        ctx = _make_context(attachments=[VOICE.__class__(**vars(VOICE))])

        await handler.handle_message(ctx, _make_channel())

        assert len(ctx.attachments) == 1

    async def test_ordinary_file_is_untouched_by_the_voice_branch(self, tmp_path):
        downloaded = tmp_path / "ok.txt"
        downloaded.write_text("content")
        coord = _coordinator()
        handler = _make_handler(coord, audio_service=_transcriber())
        doc = FileAttachment(
            url="https://x/ok.txt", mime_type="text/plain", filename="ok.txt",
        )
        ctx = _make_context(text="посмотри", attachments=[doc])

        await handler.handle_message(ctx, _make_channel(download_result=str(downloaded)))

        assert ctx.attachments == [doc]
        assert ctx.text == "посмотри"
