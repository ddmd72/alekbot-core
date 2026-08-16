"""Telegram: a voice note is speech; an uploaded audio track is a file.

`message.voice` is hold-to-record speech and carries no `file_name` — the synthetic name must
keep an `.ogg` extension because the transcription API infers the container format from it.
`message.audio` is an uploaded track and stays an ordinary attachment.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.domain.user import UserProfile
from src.ports.platform_auth_port import IAMDecision


@pytest.fixture
def adapter():
    with patch("src.adapters.telegram.webhook_adapter.Bot") as MockBot:
        MockBot.return_value = AsyncMock()
        instance = TelegramWebhookAdapter(
            token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            webhook_secret="test_webhook_secret_32chars_min",
            dedup_store=AsyncMock(),
            session_store=AsyncMock(),
            conversation_handler=AsyncMock(),
            iam_service=AsyncMock(),
        )
    return instance


def _voice_obj(mime_type="audio/ogg"):
    """Telegram Voice: file_id, duration, mime_type — and no file_name."""
    voice = MagicMock(spec=["file_id", "duration", "mime_type", "file_size"])
    voice.file_id = "voice_1"
    voice.duration = 7
    voice.mime_type = mime_type
    voice.file_size = 12_000
    return voice


def _file_info(path="https://api.telegram.org/file/bot123/voice/file_1.oga"):
    info = MagicMock()
    info.file_path = path
    return info


class TestVoiceTranslation:

    async def test_voice_gets_a_synthetic_ogg_name(self, adapter):
        adapter.bot.get_file = AsyncMock(return_value=_file_info())

        [attachment] = await adapter._translate_platform_files([_voice_obj()], is_voice=True)

        assert attachment.filename == "voice.ogg"

    async def test_voice_is_marked_as_speech(self, adapter):
        adapter.bot.get_file = AsyncMock(return_value=_file_info())

        [attachment] = await adapter._translate_platform_files([_voice_obj()], is_voice=True)

        assert attachment.is_voice_message is True

    async def test_voice_keeps_its_mime_type(self, adapter):
        adapter.bot.get_file = AsyncMock(return_value=_file_info())

        [attachment] = await adapter._translate_platform_files([_voice_obj()], is_voice=True)

        assert attachment.mime_type == "audio/ogg"

    async def test_ordinary_file_is_not_marked_as_speech(self, adapter):
        doc = MagicMock()
        doc.file_id = "doc_1"
        doc.file_name = "report.pdf"
        doc.mime_type = "application/pdf"
        doc.file_size = 2048
        adapter.bot.get_file = AsyncMock(return_value=_file_info("https://x/report.pdf"))

        [attachment] = await adapter._translate_platform_files([doc])

        assert attachment.is_voice_message is False
        assert attachment.filename == "report.pdf"


class TestVoiceExtractionFromUpdate:
    """The branch that decides which message field carries the file."""

    def _message(self, **fields):
        message = MagicMock()
        message.from_user = MagicMock(id=123456789)
        message.chat = MagicMock(id=123456789)
        message.text = None
        message.caption = None
        message.is_topic_message = False
        message.photo = None
        message.document = None
        message.voice = None
        message.audio = None
        for key, value in fields.items():
            setattr(message, key, value)
        return message

    async def _run(self, adapter, message):
        adapter.dedup_store.try_mark_processed = AsyncMock(return_value=True)
        adapter.iam_service.authorize.return_value = IAMDecision(
            action="allow",
            user=UserProfile(
                user_id="user_123", email="t@example.com", account_id="account_456",
            ),
        )
        handler = AsyncMock()
        adapter.conversation_handler = handler
        adapter.bot.get_file = AsyncMock(return_value=_file_info())

        update = MagicMock()
        update.update_id = 99001
        update.message = message

        request_obj = MagicMock()
        request_obj.headers.get.return_value = adapter.webhook_secret
        request_obj.get_json = AsyncMock(return_value={"update_id": 99001})

        with patch("src.adapters.telegram.webhook_adapter.jsonify", side_effect=lambda x: x):
            with patch("src.adapters.telegram.webhook_adapter.request", new=request_obj):
                with patch("src.adapters.telegram.webhook_adapter.Update") as update_cls:
                    update_cls.de_json.return_value = update
                    await adapter._handle_telegram_update()

        return handler.handle_message.call_args[0][0]

    async def test_voice_note_reaches_the_handler_marked_as_speech(self, adapter):
        context = await self._run(adapter, self._message(voice=_voice_obj()))

        assert len(context.attachments) == 1
        assert context.attachments[0].is_voice_message is True

    async def test_uploaded_audio_track_is_not_speech(self, adapter):
        track = MagicMock()
        track.file_id = "audio_1"
        track.file_name = "song.mp3"
        track.mime_type = "audio/mpeg"
        track.file_size = 4096

        context = await self._run(adapter, self._message(audio=track))

        assert len(context.attachments) == 1
        assert context.attachments[0].is_voice_message is False

    async def test_message_without_files_has_no_attachments(self, adapter):
        context = await self._run(adapter, self._message(text="просто текст"))

        assert context.attachments == []
