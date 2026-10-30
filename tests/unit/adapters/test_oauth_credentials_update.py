"""OAuthCredentialsPort.update on FirestoreCredentialStore (access fix 1; SECRETS_AT_REST_RFC §2.4): a
refresh or a flag write replaces an existing record and never creates one, so a disconnect stands. Assertions are on the
raw stored documents and on what readers return. Doc ids are opaque (`{user_id}:{credential_id}`, §2.3), so a stored
record is found by its `user_id` + `kind` fields; `drive_id` is stored on a OneDrive record only (§2.3)."""
import asyncio
import uuid
from dataclasses import replace

import pytest

from tests.support.oauth_credentials_store import CredentialsStore, creds, delete_kind, has_kind, one

U1, U2 = str(uuid.uuid4()), str(uuid.uuid4())


@pytest.fixture
def store():
    return CredentialsStore()


async def test_update_of_a_deleted_record_writes_nothing_and_returns_false(store):
    await store.repo.save(creds(U1, "microsoft_onedrive"))
    await delete_kind(store.repo, U1, "microsoft_onedrive")
    assert await store.repo.update(creds(U1, "microsoft_onedrive", access_token="refreshed")) is False
    assert store.docs == {}
    assert await one(store.repo, U1, "microsoft_onedrive") is None
    assert await has_kind(store.repo, U1, "microsoft_onedrive") is False
    assert await store.repo.list_user_ids("microsoft_onedrive") == []


async def test_update_of_a_record_never_saved_writes_nothing(store):
    assert await store.repo.update(creds(U1, "gmail")) is False
    assert store.docs == {}


@pytest.mark.parametrize("provider, old_drive, new_drive", [("gmail", "", ""),
                                                             ("microsoft_onedrive", "D1", "D2")])
async def test_update_replaces_every_field_of_an_existing_record_and_returns_true(store, provider, old_drive,
                                                                                  new_drive):
    await store.repo.save(creds(U1, provider, needs_reconnect=True, drive_id=old_drive))
    new = creds(U1, provider, access_token="at-new", refresh_token="rt-new", scopes=["a", "b"],
                account=replace(creds(U1, provider).account, label="other@example.com"), needs_reconnect=False,
                drive_id=new_drive)
    assert await store.repo.update(new) is True
    got = await one(store.repo, U1, provider)
    assert got.credential_id                                            # the store's own id, never the argument's ""
    assert replace(got, credential_id="") == new
    assert store.doc_of(U1, provider)["needs_reconnect"] is False      # a refresh clears the flag, as a save does


async def test_update_can_raise_the_needs_reconnect_flag(store):
    await store.repo.save(creds(U1, "gmail"))
    assert await store.repo.update(creds(U1, "gmail", needs_reconnect=True)) is True
    assert (await one(store.repo, U1, "gmail")).needs_reconnect is True


async def test_update_touches_only_its_own_user_and_provider(store):
    await store.repo.save(creds(U1, "gmail"))
    await store.repo.save(creds(U1, "google_tasks"))
    await store.repo.save(creds(U2, "gmail"))
    assert await store.repo.update(creds(U1, "gmail", access_token="at-new")) is True
    assert (await one(store.repo, U1, "gmail")).access_token == "at-new"
    assert (await one(store.repo, U1, "google_tasks")).access_token == "at-old"
    assert (await one(store.repo, U2, "gmail")).access_token == "at-old"
    assert await store.repo.update(creds(U2, "google_tasks")) is False     # no record of that provider
    assert sorted((d["user_id"], d["kind"]) for d in store.docs.values()) == sorted(
        [(U1, "gmail"), (U1, "google_tasks"), (U2, "gmail")])


async def test_a_disconnect_stands_against_an_update_in_flight(store):
    await store.repo.save(creds(U1, "microsoft_onedrive", drive_id="D1"))
    read = await one(store.repo, U1, "microsoft_onedrive")
    refreshed = asyncio.Event()
    results = []

    async def refresh():
        await refreshed.wait()
        results.append(await store.repo.update(
            creds(U1, "microsoft_onedrive", access_token="refreshed", drive_id=read.drive_id)))

    async def disconnect():
        await delete_kind(store.repo, U1, "microsoft_onedrive")
        refreshed.set()

    await asyncio.gather(refresh(), disconnect())
    assert results == [False]
    assert await one(store.repo, U1, "microsoft_onedrive") is None
    assert await has_kind(store.repo, U1, "microsoft_onedrive") is False


async def test_a_new_consent_after_a_disconnect_still_connects(store):
    await store.repo.save(creds(U1, "microsoft_onedrive"))
    await delete_kind(store.repo, U1, "microsoft_onedrive")
    await store.repo.save(creds(U1, "microsoft_onedrive", access_token="consent"))
    assert await store.repo.update(creds(U1, "microsoft_onedrive", access_token="refreshed")) is True
    assert (await one(store.repo, U1, "microsoft_onedrive")).access_token == "refreshed"
