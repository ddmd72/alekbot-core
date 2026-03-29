"""
Unit tests for LocalizationService.

Coverage:
  get_file_prompt() — delegates to port
  get_status_phrases() — delegates to port
  get_entertainment_intros() — delegates to port
"""
import pytest
from unittest.mock import MagicMock

from src.domain.language import LanguageCode
from src.domain.ui_messages import StatusType
from src.services.localization_service import LocalizationService


@pytest.fixture
def port():
    p = MagicMock()
    p.get_file_prompt.return_value = "Отправьте файл"
    p.get_status_phrases.return_value = ["Думаю...", "Обрабатываю..."]
    p.get_entertainment_intros.return_value = ["Ищу в интернете..."]
    return p


@pytest.fixture
def svc(port):
    return LocalizationService(port=port)


class TestLocalizationService:

    def test_get_file_prompt_delegates_to_port(self, svc, port):
        result = svc.get_file_prompt(LanguageCode.UK, "image/png")
        port.get_file_prompt.assert_called_once_with(LanguageCode.UK, "image/png")
        assert result == "Відправте файл" or result == "Отправьте файл"

    def test_get_file_prompt_returns_port_value(self, svc, port):
        port.get_file_prompt.return_value = "Please attach a file"
        result = svc.get_file_prompt(LanguageCode.EN, "application/pdf")
        assert result == "Please attach a file"

    def test_get_status_phrases_delegates_to_port(self, svc, port):
        result = svc.get_status_phrases(LanguageCode.EN, StatusType.THINKING)
        port.get_status_phrases.assert_called_once_with(LanguageCode.EN, StatusType.THINKING)
        assert isinstance(result, list)

    def test_get_status_phrases_returns_port_value(self, svc, port):
        port.get_status_phrases.return_value = ["Thinking...", "Processing..."]
        result = svc.get_status_phrases(LanguageCode.EN, StatusType.THINKING)
        assert result == ["Thinking...", "Processing..."]

    def test_get_entertainment_intros_delegates_to_port(self, svc, port):
        result = svc.get_entertainment_intros(LanguageCode.UK)
        port.get_entertainment_intros.assert_called_once_with(LanguageCode.UK)
        assert isinstance(result, list)

    def test_get_entertainment_intros_returns_port_value(self, svc, port):
        port.get_entertainment_intros.return_value = ["Scouring the web..."]
        result = svc.get_entertainment_intros(LanguageCode.EN)
        assert result == ["Scouring the web..."]
