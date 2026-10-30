"""Access fix 1: every refresh or flag write goes through OAuthCredentialsPort.update, so a disconnect that
lands while it is in flight stands, and the caller behaves as it does for a user with no credentials.

The store is the real FirestoreOAuthCredentialsAdapter on the evaluating Firestore fake; the race is made by
`disconnect_after_read` (the disconnect lands after the caller's read) or inside the token endpoint's answer."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.google_tasks_adapter import GoogleTasksAdapter
from src.adapters.microsoft.graph_auth import MicrosoftGraphTokenProvider
from src.adapters.microsoft.onedrive_adapter import OneDriveAdapter
from src.handlers.worker_handler import WorkerHandler
from src.services.email_review_service import EmailReviewService
from src.services.email_search_service import EmailSearchService
from tests.support.oauth_credentials_store import CredentialsStore, creds, delete_kind, has_kind, one
from src.composition.connector_registry import ConnectorRegistry

USER = "7d1f0c52-3b0e-4c53-9b86-0a1f4d2e9a11"
SOON = datetime.now(timezone.utc) + timedelta(minutes=1)      # inside every refresh margin
LATE = datetime.now(timezone.utc) + timedelta(hours=1)


@pytest.fixture
def store():
    return CredentialsStore()


def _google_tasks(store) -> GoogleTasksAdapter:
    """GoogleTasksAdapter is frozen and no longer implements the whole TasksProviderPort, so it cannot be
    instantiated as is; the auth helper under test needs none of the missing methods."""
    adapter_cls = type("GoogleTasksAuthOnly", (GoogleTasksAdapter,), {})
    adapter_cls.__abstractmethods__ = frozenset()
    return adapter_cls(store.repo, "cid", "secret", connector=ConnectorRegistry().get("google_tasks"))


# ------------------------------------------------------------------------------------ Microsoft Graph tokens

def _token_endpoint_answering(store, provider, payload):
    """aiohttp.ClientSession whose token POST answers after the user disconnected."""
    async def answer():
        await delete_kind(store.repo, USER, provider)
        return payload

    resp = MagicMock(status=200)
    resp.json = AsyncMock(side_effect=answer)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post.return_value = resp
    return session


@pytest.mark.parametrize("provider", ["microsoft_onedrive", "microsoft_todo"])
async def test_a_graph_refresh_after_a_disconnect_is_not_connected_and_writes_nothing(store, provider):
    await store.repo.save(creds(USER, provider, token_expiry=SOON))
    credential_id = (await one(store.repo, USER, provider)).credential_id
    tokens = MicrosoftGraphTokenProvider(store.repo, "cid", "secret", provider, "scope")
    session = _token_endpoint_answering(store, provider, {"access_token": "new", "expires_in": 3600})
    with patch("aiohttp.ClientSession", return_value=session):
        assert await tokens.user_headers(USER) is None
    assert store.docs == {}
    assert tokens.cached_token(USER, credential_id) is None
    assert await has_kind(store.repo, USER, provider) is False


async def test_a_graph_refresh_of_a_connected_grant_is_stored_and_served(store):
    await store.repo.save(creds(USER, "microsoft_onedrive", token_expiry=SOON, drive_id="D1"))
    tokens = MicrosoftGraphTokenProvider(store.repo, "cid", "secret", "microsoft_onedrive", "scope")
    resp = MagicMock(status=200)
    resp.json = AsyncMock(return_value={"access_token": "new", "expires_in": 3600})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post.return_value = resp
    with patch("aiohttp.ClientSession", return_value=session):
        assert await tokens.user_headers(USER) == {"Authorization": "Bearer new"}
    stored = await one(store.repo, USER, "microsoft_onedrive")
    assert (stored.access_token, stored.drive_id) == ("new", "D1")


# --------------------------------------------------------------------------------------- OneDrive reconnect flag

async def test_flagging_a_grant_disconnected_meanwhile_returns_false_and_brings_nothing_back(store):
    await store.repo.save(creds(USER, "microsoft_onedrive"))
    store.disconnect_after_read(USER, "microsoft_onedrive")
    adapter = OneDriveAdapter(store.repo, "cid", "secret", connector=ConnectorRegistry().get("microsoft_onedrive"))
    assert await adapter.flag_reconnect(USER) is False
    assert store.docs == {}


async def test_flagging_a_connected_grant_stores_the_flag(store):
    await store.repo.save(creds(USER, "microsoft_onedrive", drive_id="D1"))
    adapter = OneDriveAdapter(store.repo, "cid", "secret", connector=ConnectorRegistry().get("microsoft_onedrive"))
    assert await adapter.flag_reconnect(USER) is True
    stored = await one(store.repo, USER, "microsoft_onedrive")
    assert (stored.needs_reconnect, stored.drive_id) == (True, "D1")


# ------------------------------------------------------------------------------------------------ Google Tasks

async def test_google_tasks_refresh_after_a_disconnect_reads_as_no_credentials(store):
    await store.repo.save(creds(USER, "google_tasks", token_expiry=SOON))
    adapter = _google_tasks(store)

    async def refresh(c):
        await delete_kind(store.repo, USER, "google_tasks")
        return creds(USER, "google_tasks", access_token="new", token_expiry=LATE)

    adapter._refresh_token = refresh
    with pytest.raises(ValueError, match="No Google Tasks credentials found"):
        await adapter._get_fresh_credentials(USER)
    assert store.docs == {}


async def test_google_tasks_refresh_of_a_connected_grant_is_stored(store):
    await store.repo.save(creds(USER, "google_tasks", token_expiry=SOON))
    adapter = _google_tasks(store)
    adapter._refresh_token = AsyncMock(return_value=creds(USER, "google_tasks", access_token="new", token_expiry=LATE))
    assert (await adapter._get_fresh_credentials(USER)).access_token == "new"
    assert (await one(store.repo, USER, "google_tasks")).access_token == "new"


# ---------------------------------------------------------------------------------------------- email search

def _gmail_refreshing_after_a_disconnect(store):
    gmail = MagicMock()

    async def refresh(c):
        await delete_kind(store.repo, USER, "gmail")
        return creds(USER, "gmail", access_token="new", token_expiry=LATE)

    gmail.refresh_token = refresh
    gmail.batch_get_full_content = AsyncMock(return_value={})
    return gmail


async def test_email_details_after_a_disconnect_during_refresh_say_gmail_is_not_connected(store):
    await store.repo.save(creds(USER, "gmail", token_expiry=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    gmail = _gmail_refreshing_after_a_disconnect(store)
    service = EmailSearchService(MagicMock(), store.repo, gmail, MagicMock(), connector=ConnectorRegistry().get("gmail"))
    answer = await service.get_details("e1", USER)
    assert answer.startswith("[Error: Gmail is not connected for this user.")
    gmail.batch_get_full_content.assert_not_called()
    assert store.docs == {}


async def test_email_attachment_after_a_disconnect_during_refresh_say_gmail_is_not_connected(store):
    await store.repo.save(creds(USER, "gmail", token_expiry=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    gmail = _gmail_refreshing_after_a_disconnect(store)
    service = EmailSearchService(MagicMock(), store.repo, gmail, MagicMock(), connector=ConnectorRegistry().get("gmail"))
    assert await service.get_attachment("e1", "a.pdf", USER) == "[Error: Gmail is not connected for this user.]"
    gmail.batch_get_full_content.assert_not_called()
    assert store.docs == {}


async def test_email_details_refresh_of_a_connected_grant_is_stored(store):
    await store.repo.save(creds(USER, "gmail", token_expiry=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    gmail = MagicMock()
    gmail.refresh_token = AsyncMock(return_value=creds(USER, "gmail", access_token="new", token_expiry=LATE))
    gmail.batch_get_full_content = AsyncMock(return_value={})
    await EmailSearchService(MagicMock(), store.repo, gmail, MagicMock(), connector=ConnectorRegistry().get("gmail")).get_details("e1", USER)
    assert (await one(store.repo, USER, "gmail")).access_token == "new"


# ----------------------------------------------------------------------------------------------- email review

async def test_the_review_payload_is_none_when_gmail_is_disconnected_during_refresh(store):
    await store.repo.save(creds(USER, "gmail", token_expiry=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    provider = _gmail_refreshing_after_a_disconnect(store)
    provider.list_emails = AsyncMock(return_value=([], None))
    assert await EmailReviewService(provider, store.repo, connector=ConnectorRegistry().get("gmail")).fetch_review_payload(USER) is None
    provider.list_emails.assert_not_called()
    assert store.docs == {}


async def test_the_review_refresh_of_a_connected_grant_is_stored(store):
    await store.repo.save(creds(USER, "gmail", token_expiry=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    provider = MagicMock()
    provider.refresh_token = AsyncMock(return_value=creds(USER, "gmail", access_token="new", token_expiry=LATE))
    provider.list_emails = AsyncMock(return_value=([], None))
    assert await EmailReviewService(provider, store.repo, connector=ConnectorRegistry().get("gmail")).fetch_review_payload(USER) == []
    assert (await one(store.repo, USER, "gmail")).access_token == "new"


# ----------------------------------------------------------------------------------------------------- worker

def _worker(store, notify):
    notification = AsyncMock()
    notification.notify = notify
    return WorkerHandler(
        agent_worker_handler=MagicMock(), email_indexing_service=MagicMock(), notification_service=notification,
        consolidation_service=None, coordinator=MagicMock(), agent_factory=AsyncMock(), indexed_email_repo=None,
        user_repo=MagicMock(), task_dispatch=AsyncMock(), email_review=MagicMock(), oauth_credentials=store.repo, mail_connector=ConnectorRegistry().get("gmail"),
    )


async def test_a_gmail_grant_disconnected_before_the_flag_is_not_flagged_and_nobody_is_told(store):
    await store.repo.save(creds(USER, "gmail"))
    store.disconnect_after_read(USER, "gmail")
    notify = AsyncMock()
    await _worker(store, notify)._flag_gmail_reconnect(USER, "acc")
    assert store.docs == {}
    notify.assert_not_called()


async def test_a_revoked_gmail_grant_is_flagged_and_the_user_told_once(store):
    await store.repo.save(creds(USER, "gmail"))
    notify = AsyncMock()
    worker = _worker(store, notify)
    await worker._flag_gmail_reconnect(USER, "acc")
    await worker._flag_gmail_reconnect(USER, "acc")
    assert (await one(store.repo, USER, "gmail")).needs_reconnect is True
    assert notify.await_count == 1
