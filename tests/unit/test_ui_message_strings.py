"""
UIMessage localization — fixed single-string UI messages.

Covers the delta that routed previously hardcoded UI strings (response-ready
placeholder, truncation suffix, empty-response placeholder, unknown-command
reply, new-topic ack) through LocalizationPort. Channels and the handler fall
back to the Ukrainian literals when no localization is wired (legacy default).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.slack.channel_history import SlackChannelHistorySource
from src.adapters.slack.response_channel import (
    SLACK_CHUNK_SIZE,
    SLACK_MAX_MESSAGE_LENGTH,
    SlackResponseChannel,
)
from src.adapters.telegram.response_channel import TelegramResponseChannel
from src.domain.language import LanguageCode
from src.domain.messaging import MessageContext
from src.domain.ui_messages import UIMessage
from src.handlers.conversation_handler import ConversationHandler
from src.locales import en, es, fr, uk
from src.ports.localization_port import LocalizationPort
from src.services.localization_service import LocalizationService


# ---------------------------------------------------------------------------
# Locale modules
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-UI-06")
@pytest.mark.parametrize("mod", [uk, en, fr, es], ids=["uk", "en", "fr", "es"])
def test_locale_ui_strings_cover_every_ui_message(mod):
    assert set(mod.UI_STRINGS.keys()) == {m.value for m in UIMessage}


@pytest.mark.requirement("REQ-UI-06")
@pytest.mark.parametrize("mod", [uk, en, fr, es], ids=["uk", "en", "fr", "es"])
def test_locale_unknown_command_is_a_format_template(mod):
    rendered = mod.UI_STRINGS[UIMessage.UNKNOWN_COMMAND.value].format(command="foo")
    assert "`foo`" in rendered


# ---------------------------------------------------------------------------
# FileLocalizationAdapter
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-UI-06")
def test_adapter_get_ui_string_per_language():
    adapter = FileLocalizationAdapter()
    assert adapter.get_ui_string(LanguageCode.UK, UIMessage.RESPONSE_READY) == "✅ Відповідь готова."
    assert adapter.get_ui_string(LanguageCode.EN, UIMessage.RESPONSE_READY) == "✅ Response ready."


@pytest.mark.requirement("REQ-UI-06")
def test_adapter_get_ui_string_unknown_language_uses_default_module():
    adapter = FileLocalizationAdapter()
    expected = en.UI_STRINGS[UIMessage.RESPONSE_READY.value]
    assert adapter.get_ui_string(None, UIMessage.RESPONSE_READY) == expected


@pytest.mark.requirement("REQ-UI-06")
def test_adapter_variants_contain_every_language_rendering():
    adapter = FileLocalizationAdapter()
    variants = adapter.get_ui_string_variants(UIMessage.RESPONSE_READY)
    for mod in (uk, en, fr, es):
        assert mod.UI_STRINGS[UIMessage.RESPONSE_READY.value] in variants
    assert len(variants) == len(set(variants))  # deduplicated


# ---------------------------------------------------------------------------
# LocalizationService
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-UI-06")
def test_service_get_ui_string_delegates_to_port():
    port = MagicMock(spec=LocalizationPort)
    port.get_ui_string.return_value = "stub"
    service = LocalizationService(port)
    assert service.get_ui_string(LanguageCode.EN, UIMessage.NEW_TOPIC_ACK) == "stub"
    port.get_ui_string.assert_called_once_with(LanguageCode.EN, UIMessage.NEW_TOPIC_ACK)


# ---------------------------------------------------------------------------
# Slack response channel
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-UI-08")
@pytest.mark.asyncio
async def test_slack_multichunk_header_localized():
    channel = SlackResponseChannel(
        AsyncMock(), "C1", "token",
        language=LanguageCode.EN, localization=FileLocalizationAdapter(),
    )
    channel.update_message = AsyncMock()
    channel.send_message = AsyncMock()

    await channel.send_chunked_message("A" * (SLACK_CHUNK_SIZE + 25), "msg-1")

    channel.update_message.assert_called_once_with("msg-1", "✅ Response ready.")


@pytest.mark.requirement("REQ-UI-08")
@pytest.mark.asyncio
async def test_slack_multichunk_header_defaults_to_uk_without_localization():
    channel = SlackResponseChannel(AsyncMock(), "C1", "token")
    channel.update_message = AsyncMock()
    channel.send_message = AsyncMock()

    await channel.send_chunked_message("A" * (SLACK_CHUNK_SIZE + 25), "msg-1")

    channel.update_message.assert_called_once_with("msg-1", "✅ Відповідь готова.")


@pytest.mark.requirement("REQ-UI-08")
@pytest.mark.asyncio
async def test_slack_truncation_suffix_localized():
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={})
    channel = SlackResponseChannel(
        client, "C1", "token",
        language=LanguageCode.EN, localization=FileLocalizationAdapter(),
    )

    await channel.send_message("A" * (SLACK_MAX_MESSAGE_LENGTH + 100))

    sent_text = client.chat_postMessage.call_args.kwargs["text"]
    assert sent_text.endswith("\n\n... (response truncated)")


# ---------------------------------------------------------------------------
# Telegram response channel
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-UI-06")
def test_telegram_ui_string_localized_and_fallback():
    localized = TelegramResponseChannel(
        MagicMock(), 1, language=LanguageCode.ES, localization=FileLocalizationAdapter(),
    )
    assert localized._ui_string(UIMessage.RESPONSE_READY) == "✅ Respuesta lista."

    legacy = TelegramResponseChannel(MagicMock(), 1)
    assert legacy._ui_string(UIMessage.RESPONSE_READY) == "✅ Відповідь готова."


@pytest.mark.requirement("REQ-UI-08")
@pytest.mark.asyncio
async def test_telegram_multichunk_header_localized():
    channel = TelegramResponseChannel(
        MagicMock(), 1, language=LanguageCode.EN, localization=FileLocalizationAdapter(),
    )
    channel.update_message = AsyncMock()
    channel.send_message = AsyncMock()

    long_text = "word " * 1200  # > TELEGRAM_CHUNK_SIZE → multiple chunks
    await channel.send_chunked_message(long_text, "42")

    channel.update_message.assert_called_once_with("42", "✅ Response ready.")


# ---------------------------------------------------------------------------
# ConversationHandler._ui_string
# ---------------------------------------------------------------------------

def _make_handler(localization) -> ConversationHandler:
    # _ui_string only touches self._localization — bypass the heavy constructor.
    handler = ConversationHandler.__new__(ConversationHandler)
    handler._localization = localization
    return handler


def _make_context(language: str) -> MessageContext:
    return MessageContext(
        text="", session_id="s", user_id="u", account_id="a", language=language,
    )


@pytest.mark.requirement("REQ-UI-06")
def test_handler_ui_string_localized_for_context_language():
    handler = _make_handler(LocalizationService(FileLocalizationAdapter()))
    ctx = _make_context("en")
    assert handler._ui_string(ctx, UIMessage.EMPTY_MODEL_RESPONSE) == "*(empty response from the model)*"
    assert handler._ui_string(ctx, UIMessage.UNKNOWN_COMMAND, command="x") == "Unknown command: `x`"


@pytest.mark.requirement("REQ-UI-06")
def test_handler_ui_string_falls_back_to_uk_without_localization():
    handler = _make_handler(None)
    ctx = _make_context("en")
    assert handler._ui_string(ctx, UIMessage.EMPTY_MODEL_RESPONSE) == "*(порожня відповідь від моделі)*"


# ---------------------------------------------------------------------------
# Slack channel history filter
# ---------------------------------------------------------------------------

@pytest.mark.requirement("REQ-UI-06")
@pytest.mark.asyncio
async def test_history_filters_response_ready_in_every_language():
    ready_variants = [
        mod.UI_STRINGS[UIMessage.RESPONSE_READY.value] for mod in (uk, en, fr, es)
    ]
    raw = [{"text": "current input", "user": "U1", "ts": "9.0"}]
    raw += [
        {"text": variant, "bot_id": "B1", "ts": str(8.0 - i)}
        for i, variant in enumerate(ready_variants)
    ]
    raw.append({"text": "real reply", "bot_id": "B1", "ts": "1.0"})

    client = AsyncMock()
    client.conversations_history = AsyncMock(return_value={"messages": raw})
    source = SlackChannelHistorySource(client, bot_user_id="UBOT")

    messages = await source.fetch("C1")

    texts = [part.text for m in messages for part in m.parts]
    assert texts == ["real reply"]
