"""The credential store on the re-keyed port (MULTI_DRIVE_RFC §4.1, §4.2, E15): any number of records per kind, records
found by credential, `integration` and `account_id` persisted in plaintext and kept by update, a record written before
them read with both missing, and the cross-user listings. `FirestoreCredentialStore` over the evaluating Firestore fake
(`EvalDb`, index refusal on) and the AES-GCM test cipher; assertions on the raw stored documents and on what readers
return."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.composition.connector_registry import ConnectorRegistry
from src.domain.credential_key import CredentialKey
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
CREDS = "eval_credentials"
EXPIRY = datetime(2026, 10, 10, 11, 12, 13, tzinfo=timezone.utc)
ALEX, CLARA = "user-alex", "user-clara"
REGISTRY = ConnectorRegistry()


class Store:
    def __init__(self, cipher=None, db=None) -> None:
        self.db = db or EvalDb(indexes=INDEXES_FILE, groups={CREDS: "development_credentials"})
        self.cipher = cipher or AesGcmTestCipher()
        self.repo = FirestoreCredentialStore(self.db, SimpleNamespace(credentials_collection=CREDS), cipher=self.cipher)

    @property
    def docs(self) -> dict:
        return self.db.collection(CREDS).docs

    def doc(self, credential_id: str) -> dict:
        (found,) = [d for d in self.docs.values() if d["credential_id"] == credential_id]
        return found


class NoDecrypt(AesGcmTestCipher):
    """A cipher whose decrypt fails: a reader that calls it raises."""

    async def decrypt(self, ciphertext: str, *, aad: str) -> str:
        raise RuntimeError("decrypt called")


@pytest.fixture
def store() -> Store:
    return Store()


def cred(user_id=ALEX, kind="microsoft_onedrive", *, subject="sub-1", label="alex@example.com",
         account_id="acct-1", **over) -> OAuthCredential:
    spec = REGISTRY.get(kind)
    fields = dict(user_id=user_id, account_id=account_id, kind=kind, integration=spec.integration,
                  account=ProviderAccount(realm=spec.realm, subject=subject, label=label),
                  access_token="at-1", refresh_token="rt-1", token_expiry=EXPIRY, scopes=["s.read"])
    fields.update(over)
    return OAuthCredential(**fields)


async def _ids(store: Store, user_id: str, kind=None) -> list:
    return [c.credential_id for c in await store.repo.list_for_user(user_id, kind)]


# ------------------------------------------------------------------ cardinality (E15)

async def test_two_accounts_of_one_kind_are_two_records(store):
    await store.repo.save(cred(subject="sub-1", label="alex@example.com"))
    await store.repo.save(cred(subject="sub-2", label="user@example.com"))
    got = await store.repo.list_for_user(ALEX, "microsoft_onedrive")
    assert sorted(c.account.subject for c in got) == ["sub-1", "sub-2"]
    assert len({c.credential_id for c in got}) == 2 and len(store.docs) == 2


async def test_a_consent_of_the_same_account_keeps_its_record(store):
    await store.repo.save(cred(subject="sub-1"))
    await store.repo.save(cred(subject="sub-2"))
    before = await _ids(store, ALEX)
    await store.repo.save(cred(subject="sub-1", access_token="at-again"))
    assert sorted(await _ids(store, ALEX)) == sorted(before)
    again = [c for c in await store.repo.list_for_user(ALEX) if c.account.subject == "sub-1"]
    assert [c.access_token for c in again] == ["at-again"]


async def test_list_for_user_filters_by_kind_and_returns_all_without_one(store):
    await store.repo.save(cred(kind="gmail", subject="sub-g"))
    await store.repo.save(cred(kind="microsoft_todo", subject="sub-m"))
    await store.repo.save(cred(kind="microsoft_onedrive", subject="sub-m"))
    await store.repo.save(cred(CLARA, kind="gmail", subject="sub-c", label="clara@example.com"))
    assert sorted(c.kind for c in await store.repo.list_for_user(ALEX)) == [
        "gmail", "microsoft_onedrive", "microsoft_todo"]
    assert [c.kind for c in await store.repo.list_for_user(ALEX, "microsoft_todo")] == ["microsoft_todo"]
    assert [c.user_id for c in await store.repo.list_for_user(CLARA)] == [CLARA]
    assert await store.repo.list_for_user(ALEX, "google_tasks") == []


@pytest.mark.parametrize("older", ["sub-1", "sub-2"])
async def test_list_for_user_is_oldest_first(store, older):
    await store.repo.save(cred(subject="sub-1"))
    await store.repo.save(cred(subject="sub-2"))
    for doc in store.docs.values():
        doc["created_at"] = EXPIRY if doc["account"]["subject"] == older else EXPIRY + timedelta(days=1)
    first, second = await store.repo.list_for_user(ALEX, "microsoft_onedrive")
    assert (first.account.subject, second.account.subject) == (older, ({"sub-1", "sub-2"} - {older}).pop())


# ------------------------------------------------------------------ get / delete by credential

async def test_get_reads_one_record_by_credential(store):
    await store.repo.save(cred(subject="sub-1", access_token="at-a"))
    await store.repo.save(cred(subject="sub-2", access_token="at-b"))
    for c in await store.repo.list_for_user(ALEX):
        got = await store.repo.get(ALEX, c.credential_id)
        assert got == c
    assert await store.repo.get(ALEX, "cred-1") is None
    assert await store.repo.get(ALEX, "") is None


async def test_get_never_returns_another_users_record(store):
    await store.repo.save(cred(CLARA, subject="sub-c"))
    (clara,) = await store.repo.list_for_user(CLARA)
    assert await store.repo.get(ALEX, clara.credential_id) is None


async def test_a_record_stored_under_another_user_is_not_served(store):
    """The doc id names the user; a document whose stored user differs (a broken invariant) is never returned."""
    await store.repo.save(cred(CLARA, subject="sub-c"))
    (clara,) = await store.repo.list_for_user(CLARA)
    store.docs[f"{ALEX}:{clara.credential_id}"] = dict(store.doc(clara.credential_id))
    assert await store.repo.get(ALEX, clara.credential_id) is None


async def test_delete_removes_only_that_credential_and_is_idempotent(store):
    await store.repo.save(cred(subject="sub-1"))
    await store.repo.save(cred(subject="sub-2"))
    await store.repo.save(cred(CLARA, subject="sub-1"))
    one, other = await store.repo.list_for_user(ALEX)
    await store.repo.delete(ALEX, one.credential_id)
    await store.repo.delete(ALEX, one.credential_id)
    assert await _ids(store, ALEX) == [other.credential_id]
    assert len(await store.repo.list_for_user(CLARA)) == 1


async def test_delete_of_another_users_credential_id_touches_nothing(store):
    await store.repo.save(cred(CLARA, subject="sub-c"))
    (clara,) = await store.repo.list_for_user(CLARA)
    await store.repo.delete(ALEX, clara.credential_id)
    assert await _ids(store, CLARA) == [clara.credential_id]


# ------------------------------------------------------------------ cross-user listings, plaintext only

async def test_list_by_account_finds_the_account_across_users(store):
    await store.repo.save(cred("user-1", kind="gmail", subject="sub-1", label="user@example.com"))
    await store.repo.save(cred("user-1", kind="google_tasks", subject="sub-1", label="user@example.com"))
    await store.repo.save(cred("user-2", kind="gmail", subject="sub-1", label="user@example.com"))
    await store.repo.save(cred(CLARA, kind="gmail", subject="sub-2", label="clara@example.com"))
    await store.repo.save(cred(ALEX, kind="microsoft_todo", subject="sub-1"))     # another realm, same subject
    reader = Store(cipher=NoDecrypt(), db=store.db)
    keys = await reader.repo.list_by_account("google", "sub-1")
    expected = [CredentialKey(user_id=c.user_id, credential_id=c.credential_id)
                for u in (ALEX, CLARA, "user-1", "user-2") for c in await store.repo.list_for_user(u)
                if c.account.realm == "google" and c.account.subject == "sub-1"]
    assert sorted(keys, key=lambda k: (k.user_id, k.credential_id)) == sorted(
        expected, key=lambda k: (k.user_id, k.credential_id))
    assert len(keys) == 3
    assert await reader.repo.list_by_account("google", "sub-9") == []


async def test_list_user_ids_lists_each_user_of_a_kind_once(store):
    await store.repo.save(cred(ALEX, subject="sub-1"))
    await store.repo.save(cred(ALEX, subject="sub-2"))
    await store.repo.save(cred(CLARA, subject="sub-3"))
    await store.repo.save(cred(CLARA, kind="gmail", subject="sub-3"))
    reader = Store(cipher=NoDecrypt(), db=store.db)
    users = await reader.repo.list_user_ids("microsoft_onedrive")
    assert sorted(users) == [ALEX, CLARA] and len(users) == 2
    assert await reader.repo.list_user_ids("gmail") == [CLARA]
    assert await reader.repo.list_user_ids("google_tasks") == []


# ------------------------------------------------------------------ integration and account_id

async def test_integration_and_account_id_are_plaintext_fields(store):
    await store.repo.save(cred(kind="gmail", subject="sub-g", account_id="acct-1"))
    (doc,) = store.docs.values()
    assert (doc["integration"], doc["account_id"], doc["kind"]) == ("mail", "acct-1", "gmail")
    assert doc["account"] == {"realm": "google", "subject": "sub-g"}
    opened = json.loads(await store.cipher.decrypt(doc["secret"], aad=f"credentials/{ALEX}:{doc['credential_id']}"))
    assert opened["account_label"] == "alex@example.com"                       # the label stays in the ciphertext
    (got,) = await store.repo.list_for_user(ALEX)
    assert (got.integration, got.account_id) == (Integration.MAIL, "acct-1")


@pytest.mark.parametrize("over", [{"integration": None}, {"account_id": None}, {"account_id": "  "},
                                  {"account": ProviderAccount(realm="google", subject="", label="")}])
async def test_a_consent_without_its_identity_stores_nothing(store, over):
    with pytest.raises(ValueError):
        await store.repo.save(cred(kind="gmail", **over))
    assert store.docs == {} and store.cipher.encrypts == 0


async def test_an_update_keeps_every_stored_identity_field(store):
    """D-P2: an update never takes identity from its argument; it is found by its credential_id only."""
    await store.repo.save(cred(kind="microsoft_onedrive", subject="sub-1", account_id="acct-1"))
    (stored,) = await store.repo.list_for_user(ALEX)
    hostile = replace(stored, account_id="acct-2", kind="gmail", integration=Integration.MAIL,
                      account=ProviderAccount(realm="google", subject="sub-9", label="user@example.com"),
                      access_token="at-2", token_expiry=EXPIRY + timedelta(hours=1), needs_reconnect=True)
    assert await store.repo.update(hostile) is True
    doc = store.doc(stored.credential_id)
    assert (doc["kind"], doc["integration"], doc["account_id"], doc["account"]) == (
        "microsoft_onedrive", "drive", "acct-1", {"realm": "microsoft", "subject": "sub-1"})
    got = await store.repo.get(ALEX, stored.credential_id)
    assert (got.access_token, got.needs_reconnect, got.account.label) == (
        "at-2", True, "user@example.com")


async def test_a_refresh_rebuilt_with_replace_keeps_the_identity(store):
    await store.repo.save(cred(kind="microsoft_onedrive", subject="sub-1"))
    (stored,) = await store.repo.list_for_user(ALEX)
    assert await store.repo.update(replace(stored, access_token="at-refreshed", needs_reconnect=False))
    got = await store.repo.get(ALEX, stored.credential_id)
    assert replace(got, access_token=stored.access_token) == stored
    assert got.access_token == "at-refreshed"


async def test_an_update_of_another_users_or_no_credential_writes_nothing(store):
    await store.repo.save(cred(CLARA, subject="sub-c"))
    (clara,) = await store.repo.list_for_user(CLARA)
    before = {k: dict(v) for k, v in store.docs.items()}
    commits = len(store.db.commits)
    assert await store.repo.update(replace(clara, user_id=ALEX, access_token="at-x")) is False
    assert await store.repo.update(replace(clara, credential_id="", access_token="at-x")) is False
    assert await store.repo.update(replace(clara, credential_id="cred-1", access_token="at-x")) is False
    assert store.docs == before and len(store.db.commits) == commits


async def test_two_credentials_of_one_kind_update_independently(store):
    await store.repo.save(cred(subject="sub-1"))
    await store.repo.save(cred(subject="sub-2"))
    one, other = await store.repo.list_for_user(ALEX)
    assert await store.repo.update(replace(one, access_token="at-one"))
    assert (await store.repo.get(ALEX, one.credential_id)).access_token == "at-one"
    assert (await store.repo.get(ALEX, other.credential_id)).access_token == "at-1"


async def test_a_record_written_before_the_fields_reads_them_as_missing(store):
    """The backfill sets them; the store never derives `integration` from the kind."""
    await store.repo.save(cred(kind="gmail", subject="sub-g"))
    (doc_id, doc), = store.docs.items()
    legacy = {k: v for k, v in doc.items() if k not in ("integration", "account_id")}
    store.docs[doc_id] = legacy
    (got,) = await store.repo.list_for_user(ALEX, "gmail")
    assert (got.integration, got.account_id) == (None, None)
    assert got.kind == "gmail" and got.account.subject == "sub-g"
    assert await store.repo.update(replace(got, access_token="at-2", integration=Integration.MAIL,
                                           account_id="acct-1"))
    assert "integration" not in store.docs[doc_id] and "account_id" not in store.docs[doc_id]
    assert (await store.repo.get(ALEX, got.credential_id)).access_token == "at-2"


# ------------------------------------------------------------------ save returns the id; list_keys never decrypts

async def test_save_returns_the_stored_records_id_and_get_finds_it(store):
    first = await store.repo.save(cred(subject="sub-1", access_token="at-a"))
    second = await store.repo.save(cred(subject="sub-2", access_token="at-b"))
    assert first and second and first != second
    assert {d["credential_id"] for d in store.docs.values()} == {first, second}
    assert f"{ALEX}:{first}" in store.docs
    assert (await store.repo.get(ALEX, first)).access_token == "at-a"
    assert (await store.repo.get(ALEX, second)).access_token == "at-b"
    assert await store.repo.save(cred(subject="sub-1", access_token="at-c")) == first   # same account, same id


async def test_list_keys_lists_the_users_records_of_a_kind_with_no_cipher_call(store):
    a = await store.repo.save(cred(subject="sub-1"))
    b = await store.repo.save(cred(subject="sub-2"))
    g = await store.repo.save(cred(kind="gmail", subject="sub-g"))
    await store.repo.save(cred(CLARA, subject="sub-c"))
    decrypts = store.cipher.decrypts
    keys = await store.repo.list_keys(ALEX, "microsoft_onedrive")
    assert {k.credential_id for k in keys} == {a, b} and all(k.user_id == ALEX for k in keys)
    assert {k.credential_id for k in await store.repo.list_keys(ALEX)} == {a, b, g}
    assert await store.repo.list_keys(ALEX, "google_tasks") == []
    assert store.cipher.decrypts == decrypts


async def test_list_keys_still_lists_when_the_cipher_fails(store):
    a = await store.repo.save(cred(kind="gmail", subject="sub-g"))
    broken = Store(cipher=NoDecrypt(), db=store.db)
    assert await broken.repo.list_keys(ALEX, "gmail") == [CredentialKey(user_id=ALEX, credential_id=a)]
    with pytest.raises(RuntimeError):
        await broken.repo.get(ALEX, a)          # a decrypt failure still raises, never "not connected"
    with pytest.raises(RuntimeError):
        await broken.repo.list_for_user(ALEX, "gmail")


@pytest.mark.parametrize("older", ["sub-1", "sub-2"])
async def test_list_keys_is_oldest_first(store, older):
    ids = {s: await store.repo.save(cred(subject=s)) for s in ("sub-1", "sub-2")}
    for doc in store.docs.values():
        doc["created_at"] = EXPIRY if doc["account"]["subject"] == older else EXPIRY + timedelta(days=1)
    keys = await store.repo.list_keys(ALEX, "microsoft_onedrive")
    assert keys[0].credential_id == ids[older] and len(keys) == 2
