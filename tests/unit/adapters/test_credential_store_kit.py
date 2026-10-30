"""Credential store at rest (SECRETS_AT_REST_RFC §2.3, §2.4, §8 Credential adapter; invariants I1, I2, I4, I5, I9):
`FirestoreCredentialStore` over the evaluating Firestore fake (`EvalDb`, index refusal ON) and the AES-GCM test cipher,
never a mock. Assertions are on the raw stored documents and on what readers return.
"""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.composition.connector_registry import ConnectorRegistry
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import delete_kind, has_kind, kinds_of, one

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
CREDS = "eval_credentials"
ENV = SimpleNamespace(credentials_collection=CREDS)
EXPIRY = datetime(2026, 10, 10, 11, 12, 13, 456789, tzinfo=timezone.utc)
SECRET_VALUES = ("at-secret-value", "rt-secret-value", "user@example.com")


class BrokenCipher(AesGcmTestCipher):
    """A cipher whose backend fails (a KMS outage): both calls raise."""

    async def encrypt(self, plaintext: str, *, aad: str) -> str:
        raise RuntimeError("cipher backend unavailable")

    async def decrypt(self, ciphertext: str, *, aad: str) -> str:
        raise RuntimeError("cipher backend unavailable")


class Store:
    def __init__(self, cipher: Any = None, db: Any = None) -> None:
        self.db = db or EvalDb(indexes=INDEXES_FILE, groups={CREDS: "development_credentials"})
        self.cipher = cipher or AesGcmTestCipher()
        self.repo = FirestoreCredentialStore(self.db, ENV, cipher=self.cipher)

    @property
    def docs(self) -> dict:
        return self.db.collection(CREDS).docs


@pytest.fixture
def store() -> Store:
    return Store()


def creds(user_id="user-a", kind="gmail", *, realm="google", subject="sub-a", label="user@example.com",
          **over) -> OAuthCredential:
    fields = dict(user_id=user_id, account_id="acct-1", kind=kind, integration=ConnectorRegistry().get(kind).integration,
                  account=ProviderAccount(realm=realm, subject=subject, label=label),
                  access_token="at-secret-value", refresh_token="rt-secret-value",
                  token_expiry=EXPIRY, scopes=["s.read", "s.write"])
    fields.update(over)
    return OAuthCredential(**fields)


def _strings(value: Any):
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


# ------------------------------------------------------------------ shape and round trip

async def test_a_saved_credential_round_trips_with_its_identity(store):
    await store.repo.save(creds())
    got = await one(store.repo, "user-a", "gmail")
    assert (got.access_token, got.refresh_token, got.account.label) == SECRET_VALUES
    assert (got.token_expiry, got.scopes, got.needs_reconnect) == (EXPIRY, ["s.read", "s.write"], False)
    assert (got.account.realm, got.account.subject) == ("google", "sub-a")
    assert got.credential_id and got.token_expiry.tzinfo is not None


async def test_the_record_has_the_final_shape_and_no_secret_in_a_plaintext_field(store):
    await store.repo.save(creds())
    (doc_id, doc), = store.docs.items()
    assert doc_id == f"user-a:{doc['credential_id']}"
    assert set(doc) == {"user_id", "credential_id", "kind", "integration", "account_id", "account", "scopes",
                        "token_expiry", "needs_reconnect", "created_at", "updated_at", "secret"}
    assert doc["kind"] == "gmail" and doc["account"] == {"realm": "google", "subject": "sub-a"}
    assert (doc["integration"], doc["account_id"]) == ("mail", "acct-1")
    plain = {k: v for k, v in doc.items() if k != "secret"}
    for value in SECRET_VALUES:
        assert all(value not in s for s in _strings(plain))
        assert value not in doc["secret"]
    opened = json.loads(await store.cipher.decrypt(doc["secret"], aad=f"credentials/{doc_id}"))
    assert opened == {"access_token": "at-secret-value", "refresh_token": "rt-secret-value",
                      "account_label": "user@example.com"}


async def test_only_a_onedrive_record_carries_drive_id_in_plaintext(store):
    await store.repo.save(creds(kind="microsoft_onedrive", realm="microsoft", drive_id="d1"))
    await store.repo.save(creds(kind="microsoft_todo", realm="microsoft", drive_id="ignored"))
    by_kind = {d["kind"]: d for d in store.docs.values()}
    assert by_kind["microsoft_onedrive"]["drive_id"] == "d1"
    assert "drive_id" not in by_kind["microsoft_todo"]
    assert (await one(store.repo, "user-a", "microsoft_onedrive")).drive_id == "d1"
    assert (await one(store.repo, "user-a", "microsoft_todo")).drive_id == ""


async def test_the_argument_credential_id_is_never_used(store):
    await store.repo.save(creds(credential_id="chosen-by-caller"))
    (doc,) = store.docs.values()
    assert doc["credential_id"] != "chosen-by-caller"


# ------------------------------------------------------------------ consent: I5, I9

async def test_a_same_account_consent_keeps_the_credential_id(store):
    await store.repo.save(creds())
    first = (await one(store.repo, "user-a", "gmail")).credential_id
    created = next(iter(store.docs.values()))["created_at"]
    await store.repo.save(creds(access_token="at-new", needs_reconnect=False))
    assert list(store.docs) == [f"user-a:{first}"]
    got = await one(store.repo, "user-a", "gmail")
    assert (got.credential_id, got.access_token) == (first, "at-new")
    assert store.docs[f"user-a:{first}"]["created_at"] == created


async def test_a_consent_clears_needs_reconnect(store):
    await store.repo.save(creds())
    flagged = replace(await one(store.repo, "user-a", "gmail"), needs_reconnect=True)
    assert await store.repo.update(flagged)
    await store.repo.save(creds())
    assert (await one(store.repo, "user-a", "gmail")).needs_reconnect is False


@pytest.mark.parametrize("subject", ["", "   "])
async def test_a_credential_without_its_account_subject_stores_nothing(store, subject):
    with pytest.raises(ValueError):
        await store.repo.save(creds(subject=subject))
    assert store.docs == {} and store.cipher.encrypts == 0


async def test_a_refused_identity_keeps_the_existing_record(store):
    await store.repo.save(creds())
    before = dict(store.docs)
    with pytest.raises(ValueError):
        await store.repo.save(creds(subject=""))
    assert store.docs == before


# ------------------------------------------------------------------ update: D-P2

async def test_a_field_by_field_refresh_keeps_the_identity(store):
    """What graph_auth / the Gmail and Tasks adapters do: a new OAuthCredential without the identity fields, addressed
    by the credential_id of the record it read (MULTI_DRIVE_RFC §4.2)."""
    await store.repo.save(creds())
    stored = await one(store.repo, "user-a", "gmail")
    rebuilt = OAuthCredential(credential_id=stored.credential_id, user_id=stored.user_id, account_id=None,
                              kind=stored.kind, integration=None,
                              account=ProviderAccount(realm="", subject="", label=stored.account.label),
                              access_token="at-refreshed",
                              refresh_token=stored.refresh_token, token_expiry=EXPIRY + timedelta(hours=1),
                              scopes=stored.scopes)
    assert await store.repo.update(rebuilt)
    got = await one(store.repo, "user-a", "gmail")
    assert (got.credential_id, got.account.realm, got.account.subject) == (
        stored.credential_id, "google", "sub-a")
    assert (got.access_token, got.token_expiry, got.account.label) == (
        "at-refreshed", EXPIRY + timedelta(hours=1), "user@example.com")
    assert list(store.docs) == [f"user-a:{stored.credential_id}"]


async def test_an_update_never_takes_identity_from_its_argument(store):
    await store.repo.save(creds())
    stored = await one(store.repo, "user-a", "gmail")
    # the credential_id is the update's address (MULTI_DRIVE_RFC §4.2): every other identity field is hostile
    hostile = replace(stored, kind="microsoft_todo", account_id="acct-2",
                      account=ProviderAccount(realm="microsoft", subject="sub-z", label=stored.account.label))
    assert await store.repo.update(hostile)
    (doc,) = store.docs.values()
    assert (doc["credential_id"], doc["account"]) == (stored.credential_id, {"realm": "google", "subject": "sub-a"})
    assert (doc["kind"], doc["account_id"]) == ("gmail", "acct-1")


async def test_a_onedrive_refresh_carries_drive_id(store):
    await store.repo.save(creds(kind="microsoft_onedrive", realm="microsoft", drive_id="d1"))
    stored = await one(store.repo, "user-a", "microsoft_onedrive")
    assert await store.repo.update(replace(stored, access_token="at-2"))
    assert (await one(store.repo, "user-a", "microsoft_onedrive")).drive_id == "d1"


async def test_an_update_after_revoke_returns_false_and_writes_nothing(store):
    await store.repo.save(creds())
    stored = await one(store.repo, "user-a", "gmail")
    await delete_kind(store.repo, "user-a", "gmail")
    commits = len(store.db.commits)
    assert await store.repo.update(replace(stored, access_token="at-late")) is False
    assert store.docs == {} and len(store.db.commits) == commits


async def test_an_update_of_no_record_returns_false(store):
    assert await store.repo.update(creds(credential_id="cred-1")) is False
    assert store.docs == {}


# ------------------------------------------------------------------ I2, I4

async def test_a_ciphertext_moved_to_another_record_does_not_decrypt(store):
    await store.repo.save(creds(user_id="user-a"))
    await store.repo.save(creds(user_id="user-b", subject="sub-b"))
    a = next(d for d in store.docs.values() if d["user_id"] == "user-a")
    b_id = next(k for k, d in store.docs.items() if d["user_id"] == "user-b")
    store.docs[b_id]["secret"] = a["secret"]
    with pytest.raises(InvalidTag):
        await one(store.repo, "user-b", "gmail")


async def test_a_decrypt_failure_propagates_and_is_never_not_connected():
    db = EvalDb()
    await Store(db=db).repo.save(creds())
    broken = Store(cipher=BrokenCipher(), db=db)
    with pytest.raises(RuntimeError):
        await one(broken.repo, "user-a", "gmail")
    assert await has_kind(broken.repo, "user-a", "gmail") is True
    assert len(broken.docs) == 1


async def test_an_encrypt_failure_on_save_writes_nothing():
    store = Store(cipher=BrokenCipher())
    with pytest.raises(RuntimeError):
        await store.repo.save(creds())
    assert store.docs == {}


async def test_an_encrypt_failure_on_update_keeps_the_record():
    db = EvalDb()
    good = Store(db=db)
    cid = await good.repo.save(creds())
    before = {k: dict(v) for k, v in good.docs.items()}
    with pytest.raises(RuntimeError):
        await Store(cipher=BrokenCipher(), db=db).repo.update(creds(credential_id=cid, access_token="at-x"))
    assert good.docs == before


async def test_another_key_cannot_open_a_record():
    db = EvalDb()
    await Store(db=db).repo.save(creds())
    with pytest.raises(InvalidTag):
        await one(Store(db=db).repo, "user-a", "gmail")


# ------------------------------------------------------------------ plaintext-only readers, tenancy

async def test_is_connected_and_listings_make_no_cipher_call():
    db = EvalDb(indexes=INDEXES_FILE, groups={CREDS: "development_credentials"})
    writer = Store(db=db)
    await writer.repo.save(creds("user-a", "gmail"))
    await writer.repo.save(creds("user-a", "microsoft_todo", realm="microsoft"))
    await writer.repo.save(creds("user-b", "gmail", subject="sub-b"))
    reader = Store(cipher=BrokenCipher(), db=db)
    assert await has_kind(reader.repo, "user-a", "gmail") is True
    assert await has_kind(reader.repo, "user-b", "microsoft_todo") is False
    assert sorted(await kinds_of(reader.repo, "user-a")) == ["gmail", "microsoft_todo"]
    assert sorted(await reader.repo.list_user_ids("gmail")) == ["user-a", "user-b"]
    assert await reader.repo.list_user_ids("microsoft_onedrive") == []


async def test_a_read_never_returns_another_users_or_kinds_record(store):
    await store.repo.save(creds("user-a", "gmail"))
    assert await one(store.repo, "user-b", "gmail") is None
    assert await one(store.repo, "user-a", "google_tasks") is None


async def test_revoke_deletes_only_that_kind_and_is_idempotent(store):
    await store.repo.save(creds("user-a", "gmail"))
    await store.repo.save(creds("user-a", "google_tasks"))
    await delete_kind(store.repo, "user-a", "gmail")
    await delete_kind(store.repo, "user-a", "gmail")
    assert [d["kind"] for d in store.docs.values()] == ["google_tasks"]
    assert await one(store.repo, "user-a", "gmail") is None


# ------------------------------------------------------------------ port contract carried over from the deleted kit
# The behaviours of `test_oauth_credentials_store_kit.py` (FirestoreOAuthCredentialsAdapter, deleted in S3, §2.7) that the
# port still promises, re-asserted over the encrypted store. Its doc-id and legacy-document tests are not carried: the
# store has neither (§2.3, §2.7).

PROVIDERS = ("gmail", "google_tasks", "microsoft_todo", "microsoft_onedrive")


def _realm(provider: str) -> str:
    return "microsoft" if provider.startswith("microsoft") else "google"


async def test_an_expiry_saved_in_another_zone_reads_back_as_plain_utc(store):
    plus3 = timezone(timedelta(hours=3))
    await store.repo.save(creds(token_expiry=datetime(2026, 10, 10, 14, 12, tzinfo=plus3)))
    got = await one(store.repo, "user-a", "gmail")
    assert got.token_expiry == datetime(2026, 10, 10, 11, 12, tzinfo=timezone.utc)
    assert got.token_expiry.utcoffset() == timedelta(0)
    assert type(got.token_expiry) is datetime


async def test_a_flagged_and_expired_record_is_returned_and_connected(store):
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await store.repo.save(creds(token_expiry=past))
    stored = await one(store.repo, "user-a", "gmail")
    assert await store.repo.update(replace(stored, needs_reconnect=True))
    got = await one(store.repo, "user-a", "gmail")
    assert got is not None and got.needs_reconnect is True and got.token_expiry == past
    assert await has_kind(store.repo, "user-a", "gmail") is True
    assert await store.repo.list_user_ids("gmail") == ["user-a"]


async def test_a_missing_record_is_none_and_not_connected_and_writes_nothing(store):
    assert await one(store.repo, "user-a", "gmail") is None
    assert await has_kind(store.repo, "user-a", "gmail") is False
    assert store.docs == {}


async def test_re_saving_what_was_read_keeps_every_field(store):
    """The consent and refresh paths re-save a credential built from a read."""
    await store.repo.save(creds(kind="microsoft_onedrive", realm="microsoft", drive_id="D1",
                                            scopes=["a", "b", "c"]))
    read = await one(store.repo, "user-a", "microsoft_onedrive")
    (doc_id, before), = {k: dict(v) for k, v in store.docs.items()}.items()
    await store.repo.save(read)
    (after_id, after), = store.docs.items()
    assert after_id == doc_id
    assert {k: v for k, v in after.items() if k not in ("secret", "updated_at")} == {
        k: v for k, v in before.items() if k not in ("secret", "updated_at")}
    assert await one(store.repo, "user-a", "microsoft_onedrive") == read


async def test_a_consent_rewrites_the_whole_record(store):
    await store.repo.save(creds())
    (doc_id, doc), = store.docs.items()
    store.docs[doc_id] = dict(doc, stale_extra="gone")
    await store.repo.save(creds(access_token="at-new"))
    assert "stale_extra" not in store.docs[doc_id]
    assert (await one(store.repo, "user-a", "gmail")).access_token == "at-new"


async def test_every_kind_of_every_user_reads_back_its_own_record(store):
    for user in ("user-a", "user-b"):
        for provider in PROVIDERS:
            await store.repo.save(creds(user, provider, access_token=f"at-{user}-{provider}",
                                                    realm=_realm(provider), subject=f"sub-{user}"))
    for user in ("user-a", "user-b"):
        for provider in PROVIDERS:
            got = await one(store.repo, user, provider)
            assert (got.user_id, got.kind, got.access_token) == (user, provider, f"at-{user}-{provider}")


async def test_revoke_keeps_the_users_other_kinds_and_other_users(store):
    for provider in PROVIDERS:
        await store.repo.save(creds("user-a", provider, realm=_realm(provider)))
    await store.repo.save(creds("user-b", "gmail", subject="sub-b"))
    before = {k: dict(v) for k, v in store.docs.items()}
    await delete_kind(store.repo, "user-a", "gmail")
    assert store.docs == {k: v for k, v in before.items() if not (v["user_id"] == "user-a" and v["kind"] == "gmail")}
    assert await has_kind(store.repo, "user-a", "gmail") is False
    assert await store.repo.list_user_ids("gmail") == ["user-b"]


async def test_a_new_consent_after_a_revoke_connects_again(store):
    onedrive = dict(kind="microsoft_onedrive", realm="microsoft")
    await store.repo.save(creds(drive_id="D1", **onedrive))
    await delete_kind(store.repo, "user-a", "microsoft_onedrive")
    await store.repo.save(creds(access_token="consent-2", drive_id="D2", **onedrive))
    got = await one(store.repo, "user-a", "microsoft_onedrive")
    assert (got.access_token, got.drive_id, got.needs_reconnect) == ("consent-2", "D2", False)
    assert await has_kind(store.repo, "user-a", "microsoft_onedrive") is True
    assert await store.repo.list_user_ids("microsoft_onedrive") == ["user-a"]


async def test_list_users_returns_every_user_of_a_kind_once(store):
    users = [f"user-{i:02d}" for i in range(25)]
    for i, user in enumerate(users):
        await store.repo.save(creds(user, "gmail", subject=f"sub-{user}"))
        await store.repo.save(creds(user, "gmail", subject=f"sub-{user}"))    # a re-consent
        if i % 3 == 0:
            flagged = replace(await one(store.repo, user, "gmail"), needs_reconnect=True)
            assert await store.repo.update(flagged)
        if i % 2:
            await store.repo.save(creds(user, "google_tasks", subject=f"sub-{user}"))
    await store.repo.save(creds("user-a", "microsoft_todo", realm="microsoft"))
    listed = await store.repo.list_user_ids("gmail")
    assert sorted(listed) == users and len(listed) == len(set(listed))
    assert await store.repo.list_user_ids("microsoft_todo") == ["user-a"]
    assert await store.repo.list_user_ids("microsoft_onedrive") == []


@pytest.mark.parametrize("call", [
    lambda repo, cid: one(repo, "user-a", "gmail"),
    lambda repo, cid: has_kind(repo, "user-a", "gmail"),
    lambda repo, cid: repo.save(creds()),
    lambda repo, cid: repo.update(creds(credential_id=cid)),
    lambda repo, cid: delete_kind(repo, "user-a", "gmail"),
    lambda repo, cid: repo.list_user_ids("gmail"),
    lambda repo, cid: kinds_of(repo, "user-a"),
])
async def test_a_backend_error_propagates_never_connected_or_not(store, call):
    cid = await store.repo.save(creds())
    store.db.collection(CREDS).fail = RuntimeError("backend down")
    # an update reads its one document by id (MULTI_DRIVE_RFC §4.2): the collection's query switch does not reach it
    store.db.raise_on_txn_read(CREDS, f"user-a:{cid}", RuntimeError("backend down"))
    with pytest.raises(RuntimeError, match="backend down"):
        await call(store.repo, cid)
