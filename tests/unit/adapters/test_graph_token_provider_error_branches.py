"""MicrosoftGraphTokenProvider credential-id lookup, the two branches slice 1a's tests left open: concurrent callers
share one lookup, and a listed credential whose record is gone reads as not connected."""
import asyncio

from src.adapters.microsoft.graph_auth import MicrosoftGraphTokenProvider
from tests.support.oauth_credentials_store import CredentialsStore, creds

USER = "user-alex"
KIND = "microsoft_onedrive"


def _provider(store: CredentialsStore) -> MicrosoftGraphTokenProvider:
    return MicrosoftGraphTokenProvider(store.repo, "cid", "csecret", KIND, "Files.ReadWrite.AppFolder offline_access")


async def test_concurrent_lookups_of_one_user_share_one_listing():
    store = CredentialsStore()
    await store.repo.save(creds(USER, KIND))
    (stored,) = await store.repo.list_for_user(USER, KIND)
    gate, listings = asyncio.Event(), []
    real_list_keys = store.repo.list_keys

    async def slow_list_keys(user_id, kind=None):
        listings.append(user_id)
        await gate.wait()
        return await real_list_keys(user_id, kind)

    store.repo.list_keys = slow_list_keys
    tokens = _provider(store)
    first = asyncio.create_task(tokens.credential_id_of(USER))
    second = asyncio.create_task(tokens.credential_id_of(USER))
    await asyncio.sleep(0)                      # both are past the cache check; one holds the lock, one waits on it
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(first, second) == [stored.credential_id, stored.credential_id]
    assert listings == [USER]


async def test_a_listed_credential_whose_record_is_gone_reads_as_not_connected():
    store = CredentialsStore()
    await store.repo.save(creds(USER, KIND))
    tokens = _provider(store)

    async def gone(user_id, credential_id):
        return None

    store.repo.get = gone                       # the listing still names it; the record is deleted under us
    assert await tokens.user_headers(USER) is None
