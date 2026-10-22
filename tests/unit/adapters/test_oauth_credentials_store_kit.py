"""OAuthCredentialsPort kit (P6 · T2): the docstring of `src/ports/oauth_credentials_port.py` checked against
`FirestoreOAuthCredentialsAdapter` on the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on the
raw stored documents and on what readers return.

`list_connected_providers` is marked "no caller in src/" and is not tested.

`store` is the one fixture that selects the backend.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_oauth_credentials_adapter import FirestoreOAuthCredentialsAdapter
from src.domain.email import OAuthCredentials
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
CREDS = "eval_oauth_credentials"
PROVIDERS = ("gmail", "google_tasks", "microsoft_todo", "microsoft_onedrive")
U1, U2 = str(uuid.uuid4()), str(uuid.uuid4())
EXPIRY = datetime(2026, 10, 10, 11, 12, 13, 456789, tzinfo=timezone.utc)
DROP = object()


class CredentialsStore:
    """The real adapter over one EvalDb with the declared indexes, its collection mapped to its index group."""

    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={CREDS: "development_oauth_credentials"})
        self.repo = FirestoreOAuthCredentialsAdapter(self.db, SimpleNamespace(oauth_credentials_collection=CREDS))

    @property
    def docs(self) -> dict:
        return self.db.collection(CREDS).docs


@pytest.fixture
def store():
    return CredentialsStore()


def creds(user_id=U1, provider="gmail", **over) -> OAuthCredentials:
    fields = dict(user_id=user_id, provider=provider, access_token=f"at-{user_id[:4]}-{provider}",
                  refresh_token=f"rt-{user_id[:4]}-{provider}", token_expiry=EXPIRY,
                  scopes=["s.read", "s.write"], email_address=f"{provider}@example.com",
                  needs_reconnect=False, drive_id="")
    fields.update(over)
    return OAuthCredentials(**fields)


def raw(store, key_user=U1, key_provider="gmail", **over) -> str:
    """Seed a raw document of (key_user, key_provider) as the adapter writes it, under its key, with `over` on top of
    the stored fields (`DROP` removes a field)."""
    c = creds(key_user, key_provider)
    doc = dict(user_id=c.user_id, provider=c.provider, access_token=c.access_token, refresh_token=c.refresh_token,
               token_expiry=c.token_expiry, scopes=list(c.scopes), email_address=c.email_address,
               needs_reconnect=c.needs_reconnect, drive_id=c.drive_id)
    for key, value in over.items():
        if value is DROP:
            doc.pop(key)
        else:
            doc[key] = value
    doc_id = f"{key_user}_{key_provider}"
    store.docs[doc_id] = doc
    return doc_id


# ---------------------------------------------------------------------------------------- save_credentials

async def test_save_stores_every_field_under_user_and_provider(store):
    await store.repo.save_credentials(creds(needs_reconnect=True, drive_id="D1"))
    assert store.docs == {f"{U1}_gmail": {
        "user_id": U1, "provider": "gmail", "access_token": f"at-{U1[:4]}-gmail", "refresh_token": f"rt-{U1[:4]}-gmail",
        "token_expiry": EXPIRY, "scopes": ["s.read", "s.write"], "email_address": "gmail@example.com",
        "needs_reconnect": True, "drive_id": "D1",
    }}


async def test_save_replaces_the_whole_record_and_clears_needs_reconnect(store):
    raw(store, needs_reconnect=True, drive_id="OLD", legacy_extra="gone", access_token="old")
    await store.repo.save_credentials(creds(access_token="new"))
    doc = store.docs[f"{U1}_gmail"]
    assert "legacy_extra" not in doc
    assert (doc["access_token"], doc["needs_reconnect"], doc["drive_id"]) == ("new", False, "")
    got = await store.repo.get_credentials(U1, "gmail")
    assert got.needs_reconnect is False and got.access_token == "new"


async def test_save_keeps_one_record_per_user_and_provider(store):
    await store.repo.save_credentials(creds(U1, "gmail"))
    await store.repo.save_credentials(creds(U1, "microsoft_onedrive"))
    await store.repo.save_credentials(creds(U2, "gmail"))
    await store.repo.save_credentials(creds(U1, "gmail", access_token="second"))
    assert set(store.docs) == {f"{U1}_gmail", f"{U1}_microsoft_onedrive", f"{U2}_gmail"}
    assert store.docs[f"{U1}_gmail"]["access_token"] == "second"
    assert store.docs[f"{U2}_gmail"]["access_token"] == f"at-{U2[:4]}-gmail"


# ----------------------------------------------------------------------------------------- get_credentials

async def test_get_returns_every_field_as_saved_with_expiry_tz_aware_utc(store):
    saved = creds(needs_reconnect=True, drive_id="D1")
    await store.repo.save_credentials(saved)
    got = await store.repo.get_credentials(U1, "gmail")
    assert got == saved
    assert got.token_expiry.tzinfo is not None and got.token_expiry.utcoffset() == timedelta(0)
    assert type(got.token_expiry) is datetime


async def test_get_converts_an_expiry_saved_in_another_zone_to_utc(store):
    kyiv = timezone(timedelta(hours=3))
    await store.repo.save_credentials(creds(token_expiry=datetime(2026, 10, 10, 14, 12, tzinfo=kyiv)))
    got = await store.repo.get_credentials(U1, "gmail")
    assert got.token_expiry == datetime(2026, 10, 10, 11, 12, tzinfo=timezone.utc)
    assert got.token_expiry.utcoffset() == timedelta(0)


async def test_get_then_save_keeps_every_field(store):
    """The refresh path re-saves what it read (worker_handler.py:1019-1025, onedrive_adapter.py:542-545)."""
    await store.repo.save_credentials(creds(needs_reconnect=True, drive_id="D1", scopes=["a", "b", "c"]))
    before = dict(store.docs[f"{U1}_gmail"])
    await store.repo.save_credentials(await store.repo.get_credentials(U1, "gmail"))
    assert store.docs[f"{U1}_gmail"] == before


async def test_get_returns_a_needs_reconnect_record_and_an_expired_one(store):
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await store.repo.save_credentials(creds(needs_reconnect=True, token_expiry=past))
    got = await store.repo.get_credentials(U1, "gmail")
    assert got is not None and got.needs_reconnect is True and got.token_expiry == past


async def test_a_missing_record_is_none_not_connected(store):
    assert await store.repo.get_credentials(U1, "gmail") is None
    assert await store.repo.is_connected(U1, "gmail") is False
    assert store.docs == {}


async def test_tenancy_another_users_record_is_never_returned(store):
    await store.repo.save_credentials(creds(U2, "gmail"))
    await store.repo.save_credentials(creds(U2, "microsoft_onedrive"))
    assert await store.repo.get_credentials(U1, "gmail") is None
    assert await store.repo.is_connected(U1, "gmail") is False
    await store.repo.save_credentials(creds(U1, "gmail"))
    mine, theirs = await store.repo.get_credentials(U1, "gmail"), await store.repo.get_credentials(U2, "gmail")
    assert (mine.user_id, mine.access_token) == (U1, f"at-{U1[:4]}-gmail")
    assert (theirs.user_id, theirs.access_token) == (U2, f"at-{U2[:4]}-gmail")


async def test_tenancy_another_providers_record_is_never_returned(store):
    await store.repo.save_credentials(creds(U1, "google_tasks"))
    assert await store.repo.get_credentials(U1, "gmail") is None
    assert await store.repo.is_connected(U1, "gmail") is False
    for provider in PROVIDERS:
        await store.repo.save_credentials(creds(U1, provider))
    for provider in PROVIDERS:
        got = await store.repo.get_credentials(U1, provider)
        assert (got.provider, got.access_token) == (provider, f"at-{U1[:4]}-{provider}")


# ------------------------------------------------------------------------------------------ legacy documents

async def test_legacy_missing_needs_reconnect_reads_false(store):
    raw(store, needs_reconnect=DROP)
    assert (await store.repo.get_credentials(U1, "gmail")).needs_reconnect is False


@pytest.mark.parametrize("value", [DROP, None])
async def test_legacy_missing_or_null_drive_id_reads_empty(store, value):
    raw(store, drive_id=value)
    assert (await store.repo.get_credentials(U1, "gmail")).drive_id == ""


@pytest.mark.parametrize("field", ["user_id", "provider", "access_token", "refresh_token", "token_expiry", "scopes",
                                   "email_address"])
async def test_legacy_missing_any_other_field_raises_and_still_counts_as_connected(store, field):
    raw(store, **{field: DROP})
    with pytest.raises(KeyError):
        await store.repo.get_credentials(U1, "gmail")
    assert await store.repo.is_connected(U1, "gmail") is True


# ------------------------------------------------------------------------------------------- is_connected

async def test_is_connected_exactly_when_a_record_exists_whatever_its_flag_or_expiry(store):
    await store.repo.save_credentials(creds(needs_reconnect=True, token_expiry=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    assert await store.repo.is_connected(U1, "gmail") is True
    assert await store.repo.is_connected(U1, "google_tasks") is False
    assert await store.repo.is_connected(U2, "gmail") is False


# -------------------------------------------------------------------------------------- revoke_credentials

async def test_revoke_deletes_the_record_and_everything_reads_not_connected(store):
    await store.repo.save_credentials(creds(U1, "microsoft_onedrive"))
    await store.repo.save_credentials(creds(U2, "microsoft_onedrive"))
    await store.repo.revoke_credentials(U1, "microsoft_onedrive")
    assert f"{U1}_microsoft_onedrive" not in store.docs
    assert await store.repo.get_credentials(U1, "microsoft_onedrive") is None
    assert await store.repo.is_connected(U1, "microsoft_onedrive") is False
    assert await store.repo.list_users_by_provider("microsoft_onedrive") == [U2]


async def test_revoke_keeps_the_users_other_providers_and_other_users(store):
    for provider in PROVIDERS:
        await store.repo.save_credentials(creds(U1, provider))
    await store.repo.save_credentials(creds(U2, "gmail"))
    before = {k: dict(v) for k, v in store.docs.items()}
    await store.repo.revoke_credentials(U1, "gmail")
    assert store.docs == {k: v for k, v in before.items() if k != f"{U1}_gmail"}


async def test_revoke_of_an_absent_record_is_a_no_op(store):
    await store.repo.save_credentials(creds(U2, "gmail"))
    before = {k: dict(v) for k, v in store.docs.items()}
    await store.repo.revoke_credentials(U1, "gmail")
    await store.repo.revoke_credentials(U1, "gmail")
    assert store.docs == before


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "known defect: save is an upsert, so a token refresh in flight during a disconnect writes the grant back — "
    "src/adapters/firestore_oauth_credentials_adapter.py:77 (graph_auth.py:123 against onedrive_adapter.py:247; P6 T1 H4)"))
async def test_a_disconnect_stands_against_a_refresh_in_flight(store):
    await store.repo.save_credentials(creds(U1, "microsoft_onedrive", drive_id="D1"))
    read = await store.repo.get_credentials(U1, "microsoft_onedrive")      # the refresh starts from this read
    refreshed = asyncio.Event()

    async def refresh():
        await refreshed.wait()                                                # the token endpoint answers after the disconnect
        await store.repo.save_credentials(creds(U1, "microsoft_onedrive", access_token="refreshed",
                                                drive_id=read.drive_id))

    async def disconnect():
        await store.repo.revoke_credentials(U1, "microsoft_onedrive")
        refreshed.set()

    await asyncio.gather(refresh(), disconnect())
    assert await store.repo.get_credentials(U1, "microsoft_onedrive") is None
    assert await store.repo.is_connected(U1, "microsoft_onedrive") is False


# ---------------------------------------------------------------------------------- list_users_by_provider

async def test_list_users_returns_every_user_of_the_provider_once_needs_reconnect_ones_included(store):
    users = [str(uuid.uuid4()) for _ in range(25)]
    for i, user in enumerate(users):
        await store.repo.save_credentials(creds(user, "gmail", needs_reconnect=(i % 3 == 0)))
        await store.repo.save_credentials(creds(user, "gmail"))           # a re-consent keeps one record
        if i % 2:
            await store.repo.save_credentials(creds(user, "google_tasks"))
    await store.repo.save_credentials(creds(U1, "microsoft_todo"))
    listed = await store.repo.list_users_by_provider("gmail")
    assert sorted(listed) == sorted(users)
    assert len(listed) == len(set(listed))
    assert await store.repo.list_users_by_provider("microsoft_todo") == [U1]
    assert await store.repo.list_users_by_provider("microsoft_onedrive") == []


async def test_list_users_includes_a_needs_reconnect_record(store):
    await store.repo.save_credentials(creds(U1, "gmail", needs_reconnect=True))
    await store.repo.save_credentials(creds(U2, "gmail"))
    assert sorted(await store.repo.list_users_by_provider("gmail")) == sorted([U1, U2])


@pytest.mark.parametrize("value", [DROP, None, ""])
async def test_list_users_skips_a_legacy_record_without_user_id(store, value):
    raw(store, U1, "gmail", user_id=value)
    raw(store, U2, "gmail")
    assert await store.repo.list_users_by_provider("gmail") == [U2]


async def test_list_users_reads_the_stored_provider_field(store):
    raw(store, U1, "gmail", provider="google_tasks")                      # stored provider, whatever the key says
    raw(store, U2, "gmail")
    assert await store.repo.list_users_by_provider("gmail") == [U2]
    assert await store.repo.list_users_by_provider("google_tasks") == [U1]


# --------------------------------------------------------------------------------------------------- errors

class _Down:
    async def get(self, *a, **kw):
        raise RuntimeError("backend down")

    async def set(self, *a, **kw):
        raise RuntimeError("backend down")

    async def delete(self, *a, **kw):
        raise RuntimeError("backend down")


@pytest.mark.parametrize("call", [
    lambda repo: repo.get_credentials(U1, "gmail"),
    lambda repo: repo.is_connected(U1, "gmail"),
    lambda repo: repo.save_credentials(creds()),
    lambda repo: repo.revoke_credentials(U1, "gmail"),
])
async def test_a_backend_error_propagates_never_connected_or_not(store, monkeypatch, call):
    await store.repo.save_credentials(creds())
    monkeypatch.setattr(store.repo.collection, "document", lambda _doc_id: _Down())
    with pytest.raises(RuntimeError, match="backend down"):
        await call(store.repo)


async def test_a_backend_error_in_list_users_propagates(store):
    await store.repo.save_credentials(creds())
    store.db.collection(CREDS).fail = RuntimeError("backend down")
    with pytest.raises(RuntimeError, match="backend down"):
        await store.repo.list_users_by_provider("gmail")
