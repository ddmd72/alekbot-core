"""EmailSearchService.list_headers / index_watermark - live headers under the connected credential, and the index frontier."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.composition.connector_registry import ConnectorRegistry
from src.domain.email import EmailMetadata, IndexingState
from src.domain.exceptions import EmailNotConnected
from src.ports.email_provider_port import EmailProviderPort
from src.ports.embedding_service import EmbeddingService
from src.ports.indexed_email_repository import IndexedEmailRepository
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.services.email_search_service import EmailSearchService
from tests.support.oauth_credentials_store import creds, mirror_get

USER = "user123"


@pytest.fixture
def repo():
    return AsyncMock(spec=IndexedEmailRepository)


@pytest.fixture
def oauth():
    return AsyncMock(spec=OAuthCredentialsPort)


@pytest.fixture
def gmail():
    return AsyncMock(spec=EmailProviderPort)


@pytest.fixture
def service(repo, oauth, gmail):
    return EmailSearchService(
        indexed_email_repo=repo, oauth_credentials=oauth, gmail_provider=gmail,
        embedding_service=AsyncMock(spec=EmbeddingService), connector=ConnectorRegistry().get("gmail"),
    )


def _meta(email_id: str) -> EmailMetadata:
    return EmailMetadata(email_id=email_id, provider="gmail", subject="Invoice FA-0137",
                         from_address="shop@example.com", date=datetime(2026, 3, 4, 10, 30), labels=[], snippet="s")


async def test_list_headers_passes_the_connected_credential_and_arguments(service, oauth, gmail):
    credential = creds(USER, "gmail", token_expiry=datetime(2099, 1, 1))
    oauth.get.return_value = credential
    mirror_get(oauth)
    gmail.list_headers.return_value = ([_meta("m1")], True)
    after = datetime(2026, 3, 3, tzinfo=timezone.utc)

    emails, more = await service.list_headers(USER, after=after, key="FA-0137", limit=20)

    assert [e.email_id for e in emails] == ["m1"] and more is True
    args, kwargs = gmail.list_headers.call_args
    assert args[0].access_token == credential.access_token
    assert kwargs == {"after": after, "before": None, "key": "FA-0137", "limit": 20}


async def test_list_headers_without_a_credential_raises_not_connected(service, oauth, gmail):
    oauth.get.return_value = None
    mirror_get(oauth)
    oauth.list_keys.return_value = []

    with pytest.raises(EmailNotConnected):
        await service.list_headers(USER)
    gmail.list_headers.assert_not_called()


async def test_watermark_is_none_without_a_state(service, repo):
    repo.get_indexing_state.return_value = None
    assert await service.index_watermark(USER) is None


async def test_watermark_is_none_when_the_indexing_never_ran(service, repo):
    repo.get_indexing_state.return_value = IndexingState(user_id=USER, provider="gmail")
    assert await service.index_watermark(USER) is None


async def test_watermark_is_the_naive_utc_frontier_made_aware(service, repo):
    repo.get_indexing_state.return_value = IndexingState(
        user_id=USER, provider="gmail", indexed_through=datetime(2026, 3, 4, 6, 0))

    result = await service.index_watermark(USER)

    assert result == datetime(2026, 3, 4, 6, 0, tzinfo=timezone.utc)
    assert result.tzinfo is not None
    repo.get_indexing_state.assert_awaited_once_with(USER, "gmail")
