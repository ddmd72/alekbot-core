"""
Unit tests for HistorySummaryService.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.services.history_summary_service import HistorySummaryService
from src.ports.llm_port import LLMResponse


def make_service(response_text: str = None, raise_exc: Exception = None) -> HistorySummaryService:
    mock_provider = MagicMock()
    if raise_exc:
        mock_provider.generate_content = AsyncMock(side_effect=raise_exc)
    else:
        mock_response = MagicMock(spec=LLMResponse)
        mock_response.text = response_text
        mock_provider.generate_content = AsyncMock(return_value=mock_response)
    return HistorySummaryService(llm_port=mock_provider, model_name="gemini-flash-preview")


class TestHistorySummaryService:

    async def test_returns_summary_on_success(self):
        payload = json.dumps({"summary": "Short summary. 🧱"})
        service = make_service(response_text=payload)

        result = await service.summarize_model_response("Some long response text")

        assert result == "Short summary. 🧱"

    async def test_returns_none_when_summary_empty(self):
        payload = json.dumps({"summary": ""})
        service = make_service(response_text=payload)

        result = await service.summarize_model_response("Some long response text")

        assert result is None

    async def test_returns_none_when_response_text_none(self):
        service = make_service(response_text=None)

        result = await service.summarize_model_response("Some long response text")

        assert result is None

    async def test_returns_none_and_warns_on_exception(self, caplog):
        import logging
        service = make_service(raise_exc=Exception("503 UNAVAILABLE"))

        with caplog.at_level(logging.WARNING):
            result = await service.summarize_model_response("Some long response text")

        assert result is None
        assert "Summary failed" in caplog.text
        assert "503 UNAVAILABLE" in caplog.text

    async def test_trims_whitespace_from_summary(self):
        payload = json.dumps({"summary": "  Trimmed summary.  "})
        service = make_service(response_text=payload)

        result = await service.summarize_model_response("Some long response text")

        assert result == "Trimmed summary."

    async def test_no_retry_on_failure(self):
        mock_provider = MagicMock()
        mock_provider.generate_content = AsyncMock(side_effect=Exception("fail"))
        service = HistorySummaryService(llm_port=mock_provider, model_name="model")

        await service.summarize_model_response("text")

        assert mock_provider.generate_content.call_count == 1
