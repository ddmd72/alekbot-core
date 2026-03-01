"""
Unit tests for EmailSearchService.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.domain.email import EmailFullContent, IndexedEmail, OAuthCredentials
from src.ports.email_provider_port import EmailProviderPort
from src.ports.embedding_service import EmbeddingService
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.services.email_search_service import EmailSearchService, _RRF_K


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_email(email_id: str, text: str = "fact") -> IndexedEmail:
    return IndexedEmail(
        email_id=email_id,
        user_id="user123",
        account_id="acc1",
        source="gmail",
        text=text,
        tags=["travel"],
        category="travel",
        metadata={},
        subject="Test",
        from_address="a@b.com",
        email_date=datetime(2026, 1, 15),
        indexed_at=datetime(2026, 1, 15),
    )


def _make_creds(expired: bool = False) -> OAuthCredentials:
    expiry = datetime(2020, 1, 1) if expired else datetime(2099, 1, 1)
    return OAuthCredentials(
        user_id="user123",
        provider="gmail",
        access_token="tok",
        refresh_token="ref",
        token_expiry=expiry,
        scopes=["gmail.readonly"],
        email_address="user@test.com",
    )


DUMMY_VECTOR = [0.1, 0.2, 0.3]


@pytest.fixture
def mock_email_repo():
    return AsyncMock(spec=IndexedEmailRepository)


@pytest.fixture
def mock_oauth():
    return AsyncMock(spec=OAuthCredentialsPort)


@pytest.fixture
def mock_gmail():
    return AsyncMock(spec=EmailProviderPort)


@pytest.fixture
def mock_embedding():
    emb = AsyncMock(spec=EmbeddingService)
    emb.get_embedding.return_value = DUMMY_VECTOR
    return emb


@pytest.fixture
def service(mock_email_repo, mock_oauth, mock_gmail, mock_embedding):
    return EmailSearchService(
        indexed_email_repo=mock_email_repo,
        oauth_credentials=mock_oauth,
        gmail_provider=mock_gmail,
        embedding_service=mock_embedding,
    )


# ---------------------------------------------------------------------------
# _rrf_merge — pure function
# ---------------------------------------------------------------------------

class TestRrfMerge:

    def test_single_list_preserves_order(self):
        emails = [_make_email(f"e{i}") for i in range(5)]
        result = EmailSearchService._rrf_merge([emails])
        assert [e.email_id for e in result] == ["e0", "e1", "e2", "e3", "e4"]

    def test_empty_list_returns_empty(self):
        assert EmailSearchService._rrf_merge([]) == []

    def test_empty_sublists_returns_empty(self):
        assert EmailSearchService._rrf_merge([[], []]) == []

    def test_duplicate_in_two_lists_gets_higher_score(self):
        # e1 appears in both lists at rank 0 → should beat e2 that's only in one
        e1 = _make_email("e1")
        e2 = _make_email("e2")
        e3 = _make_email("e3")
        result = EmailSearchService._rrf_merge([[e1, e2], [e1, e3]])
        # e1 has double score — must be first
        assert result[0].email_id == "e1"

    def test_all_items_returned(self):
        emails = [_make_email(f"e{i}") for i in range(10)]
        result = EmailSearchService._rrf_merge([emails])
        assert len(result) == 10

    def test_rrf_score_formula(self):
        # rank 0 in a list of k=60 → score = 1/(60+0+1) = 1/61 ≈ 0.01639
        e1 = _make_email("e1")
        e2 = _make_email("e2")
        result = EmailSearchService._rrf_merge([[e1, e2]])
        assert result[0].email_id == "e1"


# ---------------------------------------------------------------------------
# vector_search
# ---------------------------------------------------------------------------

class TestVectorSearch:

    async def test_happy_path_returns_valid_json(self, service, mock_email_repo):
        emails = [_make_email("e1", "flight to Paris")]
        mock_email_repo.find_nearest.return_value = emails

        raw = await service.vector_search("flight Paris", "Paris trip", ["travel"], "user123")

        data = json.loads(raw)
        assert data["count"] == 1
        assert data["emails"][0]["email_id"] == "e1"
        assert data["emails"][0]["text"] == "flight to Paris"

    async def test_empty_results_returns_zero_count(self, service, mock_email_repo):
        mock_email_repo.find_nearest.return_value = []

        raw = await service.vector_search("nothing", "nothing", [], "user123")

        data = json.loads(raw)
        assert data == {"count": 0, "emails": []}

    async def test_calls_three_embeddings(self, service, mock_email_repo, mock_embedding):
        mock_email_repo.find_nearest.return_value = []

        await service.vector_search("primary", "alternative", ["tag1", "tag2"], "user123")

        assert mock_embedding.get_embedding.call_count == 3
        calls = [c.args[0] for c in mock_embedding.get_embedding.call_args_list]
        assert "primary" in calls
        assert "alternative" in calls
        assert "tag1 tag2" in calls

    async def test_tags_empty_fallback_to_primary(self, service, mock_email_repo, mock_embedding):
        mock_email_repo.find_nearest.return_value = []

        await service.vector_search("primary_q", "alt_q", [], "user123")

        calls = [c.args[0] for c in mock_embedding.get_embedding.call_args_list]
        # With no tags, tags_text = primary_q — should appear twice
        assert calls.count("primary_q") == 2

    async def test_calls_two_find_nearest(self, service, mock_email_repo):
        mock_email_repo.find_nearest.return_value = []

        await service.vector_search("a", "b", ["c"], "user123")

        assert mock_email_repo.find_nearest.call_count == 2

    async def test_call_b_includes_attachments_vector(self, service, mock_email_repo):
        mock_email_repo.find_nearest.return_value = []

        await service.vector_search("a", "b", ["c"], "user123")

        calls = mock_email_repo.find_nearest.call_args_list
        vectors_a = calls[0].kwargs["vectors"]
        vectors_b = calls[1].kwargs["vectors"]
        assert "attachments_vector" not in vectors_a
        assert "attachments_vector" in vectors_b

    async def test_rrf_merge_deduplicates(self, service, mock_email_repo):
        e1 = _make_email("e1")
        # Both find_nearest calls return the same email
        mock_email_repo.find_nearest.return_value = [e1]

        raw = await service.vector_search("q", "q2", ["t"], "user123")

        data = json.loads(raw)
        assert data["count"] == 1  # deduplicated

    async def test_email_date_formatted(self, service, mock_email_repo):
        mock_email_repo.find_nearest.return_value = [_make_email("e1")]

        raw = await service.vector_search("q", "q2", ["t"], "user123")

        data = json.loads(raw)
        assert data["emails"][0]["date"] == "2026-01-15"


# ---------------------------------------------------------------------------
# get_details
# ---------------------------------------------------------------------------

class TestGetDetails:

    async def test_no_credentials_returns_error(self, service, mock_oauth):
        mock_oauth.get_credentials.return_value = None

        result = await service.get_details("email_id_1", "user123")

        assert "not connected" in result.lower() or "error" in result.lower()

    async def test_email_not_found_returns_error(self, service, mock_oauth, mock_gmail):
        mock_oauth.get_credentials.return_value = _make_creds()
        mock_gmail.batch_get_full_content.return_value = {}  # empty — email missing

        result = await service.get_details("email_id_1", "user123")

        assert "not found" in result.lower() or "error" in result.lower()

    async def test_success_returns_body_preview(self, service, mock_oauth, mock_gmail):
        mock_oauth.get_credentials.return_value = _make_creds()
        full = EmailFullContent(
            email_id="e1",
            body_text="Important flight confirmation ref XYZ",
            body_html=None,
            attachments=["ticket.pdf"],
            attachment_binaries={},
        )
        mock_gmail.batch_get_full_content.return_value = {"e1": full}

        result = await service.get_details("e1", "user123")

        assert "Important flight confirmation ref XYZ" in result
        assert "ticket.pdf" in result

    async def test_expired_token_triggers_refresh(self, service, mock_oauth, mock_gmail):
        expired_creds = _make_creds(expired=True)
        fresh_creds = _make_creds(expired=False)
        mock_oauth.get_credentials.return_value = expired_creds
        mock_gmail.refresh_token.return_value = fresh_creds
        mock_gmail.batch_get_full_content.return_value = {}

        await service.get_details("e1", "user123")

        mock_gmail.refresh_token.assert_awaited_once_with(expired_creds)
        mock_oauth.save_credentials.assert_awaited_once_with(fresh_creds)

    async def test_no_attachments_no_attachments_line(self, service, mock_oauth, mock_gmail):
        mock_oauth.get_credentials.return_value = _make_creds()
        full = EmailFullContent(
            email_id="e1",
            body_text="Body only",
            body_html=None,
            attachments=[],
            attachment_binaries={},
        )
        mock_gmail.batch_get_full_content.return_value = {"e1": full}

        result = await service.get_details("e1", "user123")

        assert "Attachments" not in result


# ---------------------------------------------------------------------------
# get_attachment
# ---------------------------------------------------------------------------

class TestGetAttachment:

    async def test_no_credentials_returns_error(self, service, mock_oauth):
        mock_oauth.get_credentials.return_value = None

        result = await service.get_attachment("e1", "file.pdf", "user123")

        assert "not connected" in result.lower() or "error" in result.lower()

    async def test_email_not_found_returns_error(self, service, mock_oauth, mock_gmail):
        mock_oauth.get_credentials.return_value = _make_creds()
        mock_gmail.batch_get_full_content.return_value = {}

        result = await service.get_attachment("e1", "file.pdf", "user123")

        assert "not found" in result.lower() or "error" in result.lower()

    async def test_attachment_not_found_lists_available(self, service, mock_oauth, mock_gmail):
        mock_oauth.get_credentials.return_value = _make_creds()
        full = EmailFullContent(
            email_id="e1",
            body_text="body",
            body_html=None,
            attachments=["other.pdf"],
            attachment_binaries={"other.pdf": b"data"},
        )
        mock_gmail.batch_get_full_content.return_value = {"e1": full}

        result = await service.get_attachment("e1", "missing.pdf", "user123")

        assert "missing.pdf" in result
        assert "other.pdf" in result

    async def test_too_large_returns_size_error(self, service, mock_oauth, mock_gmail):
        mock_oauth.get_credentials.return_value = _make_creds()
        big_data = b"x" * (4 * 1024 * 1024)  # 4 MB > 3 MB limit
        full = EmailFullContent(
            email_id="e1",
            body_text="body",
            body_html=None,
            attachments=["big.pdf"],
            attachment_binaries={"big.pdf": big_data},
        )
        mock_gmail.batch_get_full_content.return_value = {"e1": full}

        result = await service.get_attachment("e1", "big.pdf", "user123")

        assert "4.0 MB" in result or "exceeds" in result

    async def test_success_returns_converted_text(self, service, mock_oauth, mock_gmail):
        mock_oauth.get_credentials.return_value = _make_creds()
        full = EmailFullContent(
            email_id="e1",
            body_text="body",
            body_html=None,
            attachments=["doc.pdf"],
            attachment_binaries={"doc.pdf": b"pdf bytes"},
        )
        mock_gmail.batch_get_full_content.return_value = {"e1": full}

        with (
            patch("src.services.email_search_service.convert_file_to_text", new_callable=AsyncMock) as mock_convert,
            patch("src.services.email_search_service._truncate_with_alert") as mock_truncate,
        ):
            mock_convert.return_value = "Parsed document text"
            mock_truncate.return_value = "Parsed document text"

            result = await service.get_attachment("e1", "doc.pdf", "user123")

        assert result == "Parsed document text"
        mock_convert.assert_awaited_once()
