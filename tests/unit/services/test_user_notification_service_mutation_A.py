"""UserNotificationService: the failure paths of best-effort calls leave a log trace and a defined return value.

Each handler swallows the error on purpose, so the log line (and, for the UI language lookup, the English fallback)
is the only observable behaviour; a handler that silently returns None hides the failure from the operator.
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.language import LanguageCode
from src.domain.messaging import SmartResponse
from src.domain.notification import NotificationChannel
from src.ports.language_service_port import LanguageServicePort
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService

_USER_ID = "user-mutA-1"
_ACCOUNT_ID = "account-mutA-1"


@pytest.fixture
def state() -> AsyncMock:
    st = AsyncMock(spec=NotificationStatePort)
    st.get.return_value = NotificationChannel(
        user_id=_USER_ID, platform="telegram", channel_id="C-mutA-1", updated_at=datetime(2026, 1, 1),
    )
    st.get_primary.return_value = None
    return st


@pytest.fixture
def channel() -> MagicMock:
    ch = MagicMock()
    ch.send_long_text = AsyncMock(side_effect=RuntimeError("boom-send"))
    ch.send_rich_content = AsyncMock()
    return ch


@pytest.fixture
def language() -> AsyncMock:
    lang = AsyncMock(spec=LanguageServicePort)
    lang.resolve_ui_language.side_effect = RuntimeError("boom-lang")
    return lang


@pytest.fixture
def service(state, channel, language) -> UserNotificationService:
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = channel
    return UserNotificationService(
        state_repo=state, channel_factory=factory, coordinator=AsyncMock(), notification_sla=MagicMock(),
        session_store=AsyncMock(), language=language,
    )


class TestFailuresAreLogged:
    async def test_save_channel_failure_is_logged(self, service, state, caplog):
        state.save.side_effect = RuntimeError("boom-save")
        with caplog.at_level("WARNING"):
            await service.save_channel(_USER_ID, "telegram", "C-mutA-1")
        assert "Failed to save channel" in caplog.text
        assert "boom-save" in caplog.text

    async def test_save_primary_failure_is_logged(self, service, state, caplog):
        state.save_primary.side_effect = RuntimeError("boom-primary")
        with caplog.at_level("WARNING"):
            await service.save_primary(_USER_ID, "telegram", "C-mutA-1")
        assert "Failed to save primary" in caplog.text
        assert "boom-primary" in caplog.text

    async def test_call_summary_delivery_failure_is_logged(self, service, caplog):
        with caplog.at_level("ERROR"):
            await service.notify_call_summary(_USER_ID, _ACCOUNT_ID, "summary-mutA", "[System: call]")
        assert "Call summary delivery failed" in caplog.text
        assert "boom-send" in caplog.text

    async def test_answer_copy_failure_is_logged(self, service, caplog):
        with caplog.at_level("ERROR"):
            await service.notify_answer_copy(_USER_ID, _ACCOUNT_ID, SmartResponse(text="answer-mutA"))
        assert "Answer copy failed" in caplog.text
        assert "boom-send" in caplog.text


class TestUiLanguageFallback:
    async def test_lookup_failure_falls_back_to_english_and_logs(self, service, caplog):
        with caplog.at_level("WARNING"):
            result = await service._ui_language(_USER_ID)
        assert result == LanguageCode.EN
        assert "UI language lookup failed" in caplog.text
