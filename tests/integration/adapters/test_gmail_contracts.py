"""
Integration tests for GmailProviderAdapter contracts.

First non-LLM application of the CapturingStub + ContractRule pattern (R18.2).
The mechanism that exists in `tests/contracts/adapter_contracts.py` for LLM
adapter SDK boundaries is propagated here to an HTTP-boundary adapter — the
same shape of test, the same rule repository, just a different captured-input
shape (request records vs SDK kwargs).
"""
from datetime import datetime, timezone

import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from src.domain.email import OAuthCredentials
from tests.contracts.adapter_contracts import (
    GMAIL_AUTHORIZATION_HEADER_PRESENT,
    GMAIL_LIST_EMAILS_PAGE_TOKEN_EXCLUDES_QUERY,
)
from tests.integration.adapters.conftest import GmailCapturingStub


def _credentials() -> OAuthCredentials:
    return OAuthCredentials(
        user_id="u1",
        provider="gmail",
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        token_expiry=datetime(2099, 1, 1, tzinfo=timezone.utc),
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        email_address="u@example.com",
    )


@pytest.mark.asyncio
async def test_gmail_list_emails_carries_authorization_header(monkeypatch):
    """Every Gmail API request — list and per-message metadata fetches — carries Bearer auth."""
    adapter = GmailProviderAdapter(client_id="cid", client_secret="csecret")
    stub = GmailCapturingStub().set_response_for(
        "/messages",
        {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": None},
    ).install(monkeypatch)

    await adapter.list_emails(credentials=_credentials(), max_results=5)

    assert stub.captured_requests, "expected at least one Gmail HTTP call"
    for req in stub.captured_requests:
        GMAIL_AUTHORIZATION_HEADER_PRESENT.validate("gmail", req)


@pytest.mark.asyncio
async def test_gmail_list_emails_omits_q_when_resuming_via_page_token(monkeypatch):
    """When list_emails is called with page_token, the /messages list call must omit q=.

    Gmail embeds the original query in pageToken; sending q= alongside silently
    overrides the embedded date filter and returns emails outside the requested
    range. This was an inline-comment-documented invariant before this test
    promoted it to an enforced contract.
    """
    adapter = GmailProviderAdapter(client_id="cid", client_secret="csecret")
    stub = GmailCapturingStub().set_response_for(
        "/messages",
        {"messages": [], "nextPageToken": None},
    ).install(monkeypatch)

    await adapter.list_emails(
        credentials=_credentials(),
        date_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 2, 1, tzinfo=timezone.utc),
        page_token="resume-token-from-previous-page",
        max_results=5,
    )

    list_calls = [r for r in stub.captured_requests if "pageToken" in r["params"]]
    assert list_calls, "expected exactly one /messages call carrying pageToken"
    for req in list_calls:
        GMAIL_LIST_EMAILS_PAGE_TOKEN_EXCLUDES_QUERY.validate("gmail", req)


@pytest.mark.asyncio
async def test_gmail_list_emails_includes_q_when_no_page_token(monkeypatch):
    """Sanity-pair: without page_token, q= IS included with the date filter.

    Pins the inverse of the above contract — the conditional behavior is the
    actual invariant, not just "always omit q=".
    """
    adapter = GmailProviderAdapter(client_id="cid", client_secret="csecret")
    stub = GmailCapturingStub().set_response_for(
        "/messages",
        {"messages": [], "nextPageToken": None},
    ).install(monkeypatch)

    await adapter.list_emails(
        credentials=_credentials(),
        date_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 2, 1, tzinfo=timezone.utc),
        max_results=5,
    )

    list_calls = [r for r in stub.captured_requests if "/messages" in r["url"] and "/messages/" not in r["url"]]
    assert len(list_calls) == 1
    assert "q" in list_calls[0]["params"], "q= must be present on first-page list call with date filter"
    assert "after:2026/01/01" in list_calls[0]["params"]["q"]
    assert "before:2026/02/01" in list_calls[0]["params"]["q"]
