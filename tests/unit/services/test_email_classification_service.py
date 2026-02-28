"""
Unit tests for EmailClassificationService.
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from src.domain.email import EmailClassificationResult, EmailMetadata
from src.domain.llm import Message, MessagePart
from src.ports.llm_service import LLMRequest, LLMService
from src.services.email_classification_service import EmailClassificationService


def _make_meta(email_id: str, subject: str = "Test", from_addr: str = "a@b.com") -> EmailMetadata:
    return EmailMetadata(
        email_id=email_id,
        provider="gmail",
        subject=subject,
        from_address=from_addr,
        date=datetime(2025, 3, 15),
        labels=[],
        snippet="Test snippet",
    )


def _make_llm_response(items: list) -> object:
    mock = AsyncMock()
    mock.text = json.dumps(items)
    return mock


@pytest.fixture
def mock_llm():
    return AsyncMock(spec=LLMService)


@pytest.fixture
def service(mock_llm):
    return EmailClassificationService(mock_llm, model_name="gemini-test")


class TestEmailClassificationService:

    async def test_classify_batch_returns_all_emails(self, service, mock_llm):
        """All input emails appear in output, even if not all returned by LLM."""
        emails = [_make_meta("id1"), _make_meta("id2")]
        mock_llm.generate_content.return_value = _make_llm_response([
            {"email_id": "id1", "valuable": True, "category": "travel",
             "fact": "User booked flight", "tags": ["flight"], "reason": "booking confirmation"},
            {"email_id": "id2", "valuable": False, "category": None,
             "fact": None, "tags": [], "reason": "marketing"},
        ])

        results = await service.classify_batch(emails, "user123")

        assert len(results) == 2
        assert results[0].email_id == "id1"
        assert results[0].valuable is True
        assert results[0].category == "travel"
        assert results[0].fact == "User booked flight"
        assert results[1].email_id == "id2"
        assert results[1].valuable is False

    async def test_classify_batch_fills_missing_emails(self, service, mock_llm):
        """If LLM omits an email_id, it is added as not-valuable."""
        emails = [_make_meta("id1"), _make_meta("id2")]
        # LLM only returns id1
        mock_llm.generate_content.return_value = _make_llm_response([
            {"email_id": "id1", "valuable": True, "category": "finance",
             "fact": "Invoice paid", "tags": ["invoice"], "reason": "receipt"},
        ])

        results = await service.classify_batch(emails, "user123")

        assert len(results) == 2
        missing = next(r for r in results if r.email_id == "id2")
        assert missing.valuable is False
        assert missing.reason == "missing_from_response"

    async def test_classify_batch_handles_invalid_json(self, service, mock_llm):
        """Graceful degradation on malformed LLM output."""
        emails = [_make_meta("id1")]
        mock_llm.generate_content.return_value.text = "not json at all"

        results = await service.classify_batch(emails, "user123")

        assert len(results) == 1
        assert results[0].valuable is False
        assert results[0].reason == "parse_error"

    async def test_classify_batch_handles_llm_error(self, service, mock_llm):
        """LLM exception → all emails returned as not-valuable."""
        emails = [_make_meta("id1"), _make_meta("id2")]
        mock_llm.generate_content.side_effect = RuntimeError("API error")

        results = await service.classify_batch(emails, "user123")

        assert len(results) == 2
        assert all(not r.valuable for r in results)
        assert all(r.reason == "classification_error" for r in results)

    async def test_classify_batch_empty_input(self, service, mock_llm):
        """Empty input → no LLM call, empty list returned."""
        results = await service.classify_batch([], "user123")

        assert results == []
        mock_llm.generate_content.assert_not_called()

    async def test_classify_batch_sends_correct_request(self, service, mock_llm):
        """Verify LLMRequest fields: model, JSON mode, temperature=0, disable_safety."""
        emails = [_make_meta("id1")]
        mock_llm.generate_content.return_value = _make_llm_response([
            {"email_id": "id1", "valuable": False, "category": None,
             "fact": None, "tags": [], "reason": "noise"},
        ])

        await service.classify_batch(emails, "user123")

        call_args = mock_llm.generate_content.call_args
        req: LLMRequest = call_args.kwargs.get("request") or call_args.args[0]
        assert req.model_name == "gemini-test"
        assert req.temperature == 0.0
        assert req.response_mime_type == "application/json"
        assert req.disable_safety is True
        assert req.system_instruction is not None
        assert len(req.messages) == 1
        assert req.messages[0].role == "user"

    async def test_classify_batch_tags_lowercased(self, service, mock_llm):
        """Tags returned by LLM are normalized to lowercase."""
        emails = [_make_meta("id1")]
        mock_llm.generate_content.return_value = _make_llm_response([
            {"email_id": "id1", "valuable": True, "category": "travel",
             "fact": "Flight booked", "tags": ["Flight", "RYANAIR"], "reason": "ok"},
        ])

        results = await service.classify_batch(emails, "user123")

        assert results[0].tags == ["flight", "ryanair"]

    async def test_classify_batch_strips_json_code_block(self, service, mock_llm):
        """Handles LLM wrapping output in ```json ... ``` block."""
        emails = [_make_meta("id1")]
        raw = '```json\n[{"email_id": "id1", "valuable": false, "category": null, "fact": null, "tags": [], "reason": "noise"}]\n```'
        mock_llm.generate_content.return_value.text = raw

        results = await service.classify_batch(emails, "user123")

        assert len(results) == 1
        assert results[0].valuable is False
