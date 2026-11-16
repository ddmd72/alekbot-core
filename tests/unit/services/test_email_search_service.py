"""
Unit tests for EmailSearchService.
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.domain.email import EmailFullContent, EmailPart
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.ports.email_provider_port import EmailProviderPort
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.services.email_search_service import EmailSearchService
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_creds(expired: bool = False) -> OAuthCredential:
    expiry = datetime(2020, 1, 1) if expired else datetime(2099, 1, 1)
    return OAuthCredential(
        user_id="user123",
        account_id="acct-1",
        kind="gmail",
        integration=Integration.MAIL,
        account=ProviderAccount(realm="google", subject="", label="user@example.test"),
        access_token="tok",
        refresh_token="ref",
        token_expiry=expiry,
        scopes=["gmail.readonly"],
    )


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
def service(mock_email_repo, mock_oauth, mock_gmail):
    return EmailSearchService(
        indexed_email_repo=mock_email_repo,
        oauth_credentials=mock_oauth,
        gmail_provider=mock_gmail,
        connector=ConnectorRegistry().get("gmail"),
    )


# ---------------------------------------------------------------------------
# get_details
# ---------------------------------------------------------------------------

class TestGetDetails:

    async def test_no_credentials_returns_error(self, service, mock_oauth):
        mock_oauth.get.return_value = None
        mirror_get(mock_oauth)

        result = await service.get_details("email_id_1", "user123")

        assert "not connected" in result.lower() or "error" in result.lower()

    async def test_email_not_found_returns_error(self, service, mock_oauth, mock_gmail):
        mock_oauth.get.return_value = _make_creds()
        mirror_get(mock_oauth)
        mock_gmail.batch_get_full_content.return_value = {}  # empty — email missing

        result = await service.get_details("email_id_1", "user123")

        assert "not found" in result.lower() or "error" in result.lower()

    async def test_success_returns_body_preview(self, service, mock_oauth, mock_gmail):
        mock_oauth.get.return_value = _make_creds()
        mirror_get(mock_oauth)
        full = EmailFullContent(
            email_id="e1",
            body_text="Important flight confirmation ref XYZ",
            body_html=None,
            attachments=["ticket.pdf"],
        )
        mock_gmail.batch_get_full_content.return_value = {"e1": full}
        # reviewer (U4a A2, D11): the attachment line comes from the one part listing, as a label carrying its ref
        mock_gmail.list_parts.return_value = [EmailPart("1", "ticket.pdf", "application/pdf", 2048, "att-1")]

        result = await service.get_details("e1", "user123")

        assert "Important flight confirmation ref XYZ" in result
        assert '[Email file: "ticket.pdf" (2KB, application/pdf) ref=email:e1/1]' in result
        mock_gmail.list_parts.assert_awaited_once()

    async def test_expired_token_triggers_refresh(self, service, mock_oauth, mock_gmail):
        expired_creds = _make_creds(expired=True)
        fresh_creds = _make_creds(expired=False)
        mock_oauth.get.return_value = expired_creds
        mirror_get(mock_oauth)
        mock_oauth.update.return_value = True        # the record still exists
        mock_gmail.refresh_token.return_value = fresh_creds
        mock_gmail.batch_get_full_content.return_value = {}

        await service.get_details("e1", "user123")

        mock_gmail.refresh_token.assert_awaited_once_with(expired_creds)
        mock_oauth.update.assert_awaited_once_with(fresh_creds)
        mock_oauth.save.assert_not_awaited()          # a refresh never creates a record

    async def test_no_attachments_no_attachments_line(self, service, mock_oauth, mock_gmail):
        mock_oauth.get.return_value = _make_creds()
        mirror_get(mock_oauth)
        full = EmailFullContent(
            email_id="e1",
            body_text="Body only",
            body_html=None,
            attachments=[],
        )
        mock_gmail.batch_get_full_content.return_value = {"e1": full}

        result = await service.get_details("e1", "user123")

        assert "Attachments" not in result
