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
from src.domain.email import OAuthCredentials
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb

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


def creds(user_id="user-a", provider="gmail", **over) -> OAuthCredentials:
    fields = dict(user_id=user_id, provider=provider, access_token="at-secret-value", refresh_token="rt-secret-value",
                  token_expiry=EXPIRY, scopes=["s.read", "s.write"], email_address="user@example.com",
                  account_realm="google", account_subject="sub-a")
    fields.update(over)
    return OAuthCredentials(**fields)


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
    await store.repo.save_credentials(creds())
    got = await store.repo.get_credentials("user-a", "gmail")
    assert (got.access_token, got.refresh_token, got.email_address) == SECRET_VALUES
    assert (got.token_expiry, got.scopes, got.needs_reconnect) == (EXPIRY, ["s.read", "s.write"], False)
    assert (got.account_realm, got.account_subject) == ("google", "sub-a")
    assert got.credential_id and got.token_expiry.tzinfo is not None


async def test_the_record_has_the_final_shape_and_no_secret_in_a_plaintext_field(store):
    await store.repo.save_credentials(creds())
    (doc_id, doc), = store.docs.items()
    assert doc_id == f"user-a:{doc['credential_id']}"
    assert set(doc) == {"user_id", "credential_id", "kind", "account", "scopes", "token_expiry", "needs_reconnect",
                        "created_at", "updated_at", "secret"}
    assert doc["kind"] == "gmail" and doc["account"] == {"realm": "google", "subject": "sub-a"}
    plain = {k: v for k, v in doc.items() if k != "secret"}
    for value in SECRET_VALUES:
        assert all(value not in s for s in _strings(plain))
        assert value not in doc["secret"]
    opened = json.loads(await store.cipher.decrypt(doc["secret"], aad=f"credentials/{doc_id}"))
    assert opened == {"access_token": "at-secret-value", "refresh_token": "rt-secret-value",
                      "account_label": "user@example.com"}


async def test_only_a_onedrive_record_carries_drive_id_in_plaintext(store):
    await store.repo.save_credentials(creds(provider="microsoft_onedrive", account_realm="microsoft", drive_id="d1"))
    await store.repo.save_credentials(creds(provider="microsoft_todo", account_realm="microsoft", drive_id="ignored"))
    by_kind = {d["kind"]: d for d in store.docs.values()}
    assert by_kind["microsoft_onedrive"]["drive_id"] == "d1"
    assert "drive_id" not in by_kind["microsoft_todo"]
    assert (await store.repo.get_credentials("user-a", "microsoft_onedrive")).drive_id == "d1"
    assert (await store.repo.get_credentials("user-a", "microsoft_todo")).drive_id == ""


async def test_the_argument_credential_id_is_never_used(store):
    await store.repo.save_credentials(creds(credential_id="chosen-by-caller"))
    (doc,) = store.docs.values()
    assert doc["credential_id"] != "chosen-by-caller"


# ------------------------------------------------------------------ consent: I5, I9

async def test_a_same_account_consent_keeps_the_credential_id(store):
    await store.repo.save_credentials(creds())
    first = (await store.repo.get_credentials("user-a", "gmail")).credential_id
    created = next(iter(store.docs.values()))["created_at"]
    await store.repo.save_credentials(creds(access_token="at-new", needs_reconnect=False))
    assert list(store.docs) == [f"user-a:{first}"]
    got = await store.repo.get_credentials("user-a", "gmail")
    assert (got.credential_id, got.access_token) == (first, "at-new")
    assert store.docs[f"user-a:{first}"]["created_at"] == created


async def test_a_consent_clears_needs_reconnect(store):
    await store.repo.save_credentials(creds())
    flagged = replace(await store.repo.get_credentials("user-a", "gmail"), needs_reconnect=True)
    assert await store.repo.update_credentials(flagged)
    await store.repo.save_credentials(creds())
    assert (await store.repo.get_credentials("user-a", "gmail")).needs_reconnect is False


async def test_another_account_replaces_the_record(store):
    await store.repo.save_credentials(creds())
    first = (await store.repo.get_credentials("user-a", "gmail")).credential_id
    await store.repo.save_credentials(creds(account_subject="sub-b", email_address="other@example.com"))
    assert len(store.docs) == 1
    got = await store.repo.get_credentials("user-a", "gmail")
    assert got.credential_id != first and (got.account_subject, got.email_address) == ("sub-b", "other@example.com")


@pytest.mark.parametrize("subject", ["", "   "])
async def test_a_credential_without_its_account_subject_stores_nothing(store, subject):
    with pytest.raises(ValueError):
        await store.repo.save_credentials(creds(account_subject=subject))
    assert store.docs == {} and store.cipher.encrypts == 0


async def test_a_refused_identity_keeps_the_existing_record(store):
    await store.repo.save_credentials(creds())
    before = dict(store.docs)
    with pytest.raises(ValueError):
        await store.repo.save_credentials(creds(account_subject=""))
    assert store.docs == before


# ------------------------------------------------------------------ update: D-P2

async def test_a_field_by_field_refresh_keeps_the_identity(store):
    """What graph_auth / the Gmail and Tasks adapters do: a new OAuthCredentials without the identity fields."""
    await store.repo.save_credentials(creds())
    stored = await store.repo.get_credentials("user-a", "gmail")
    rebuilt = OAuthCredentials(user_id=stored.user_id, provider=stored.provider, access_token="at-refreshed",
                               refresh_token=stored.refresh_token, token_expiry=EXPIRY + timedelta(hours=1),
                               scopes=stored.scopes, email_address=stored.email_address)
    assert await store.repo.update_credentials(rebuilt)
    got = await store.repo.get_credentials("user-a", "gmail")
    assert (got.credential_id, got.account_realm, got.account_subject) == (
        stored.credential_id, "google", "sub-a")
    assert (got.access_token, got.token_expiry, got.email_address) == (
        "at-refreshed", EXPIRY + timedelta(hours=1), "user@example.com")
    assert list(store.docs) == [f"user-a:{stored.credential_id}"]


async def test_an_update_never_takes_identity_from_its_argument(store):
    await store.repo.save_credentials(creds())
    stored = await store.repo.get_credentials("user-a", "gmail")
    hostile = replace(stored, credential_id="other", account_realm="microsoft", account_subject="sub-z")
    assert await store.repo.update_credentials(hostile)
    (doc,) = store.docs.values()
    assert (doc["credential_id"], doc["account"]) == (stored.credential_id, {"realm": "google", "subject": "sub-a"})


async def test_a_onedrive_refresh_carries_drive_id(store):
    await store.repo.save_credentials(creds(provider="microsoft_onedrive", account_realm="microsoft", drive_id="d1"))
    stored = await store.repo.get_credentials("user-a", "microsoft_onedrive")
    assert await store.repo.update_credentials(replace(stored, access_token="at-2"))
    assert (await store.repo.get_credentials("user-a", "microsoft_onedrive")).drive_id == "d1"


async def test_an_update_after_revoke_returns_false_and_writes_nothing(store):
    await store.repo.save_credentials(creds())
    stored = await store.repo.get_credentials("user-a", "gmail")
    await store.repo.revoke_credentials("user-a", "gmail")
    commits = len(store.db.commits)
    assert await store.repo.update_credentials(replace(stored, access_token="at-late")) is False
    assert store.docs == {} and len(store.db.commits) == commits


async def test_an_update_of_no_record_returns_false(store):
    assert await store.repo.update_credentials(creds()) is False
    assert store.docs == {}


# ------------------------------------------------------------------ I2, I4

async def test_a_ciphertext_moved_to_another_record_does_not_decrypt(store):
    await store.repo.save_credentials(creds(user_id="user-a"))
    await store.repo.save_credentials(creds(user_id="user-b", account_subject="sub-b"))
    a = next(d for d in store.docs.values() if d["user_id"] == "user-a")
    b_id = next(k for k, d in store.docs.items() if d["user_id"] == "user-b")
    store.docs[b_id]["secret"] = a["secret"]
    with pytest.raises(InvalidTag):
        await store.repo.get_credentials("user-b", "gmail")


async def test_a_decrypt_failure_propagates_and_is_never_not_connected():
    db = EvalDb()
    await Store(db=db).repo.save_credentials(creds())
    broken = Store(cipher=BrokenCipher(), db=db)
    with pytest.raises(RuntimeError):
        await broken.repo.get_credentials("user-a", "gmail")
    assert await broken.repo.is_connected("user-a", "gmail") is True
    assert len(broken.docs) == 1


async def test_an_encrypt_failure_on_save_writes_nothing():
    store = Store(cipher=BrokenCipher())
    with pytest.raises(RuntimeError):
        await store.repo.save_credentials(creds())
    assert store.docs == {}


async def test_an_encrypt_failure_on_update_keeps_the_record():
    db = EvalDb()
    good = Store(db=db)
    await good.repo.save_credentials(creds())
    before = {k: dict(v) for k, v in good.docs.items()}
    with pytest.raises(RuntimeError):
        await Store(cipher=BrokenCipher(), db=db).repo.update_credentials(creds(access_token="at-x"))
    assert good.docs == before


async def test_another_key_cannot_open_a_record():
    db = EvalDb()
    await Store(db=db).repo.save_credentials(creds())
    with pytest.raises(InvalidTag):
        await Store(db=db).repo.get_credentials("user-a", "gmail")


# ------------------------------------------------------------------ plaintext-only readers, tenancy

async def test_is_connected_and_listings_make_no_cipher_call():
    db = EvalDb(indexes=INDEXES_FILE, groups={CREDS: "development_credentials"})
    writer = Store(db=db)
    await writer.repo.save_credentials(creds("user-a", "gmail"))
    await writer.repo.save_credentials(creds("user-a", "microsoft_todo", account_realm="microsoft"))
    await writer.repo.save_credentials(creds("user-b", "gmail", account_subject="sub-b"))
    reader = Store(cipher=BrokenCipher(), db=db)
    assert await reader.repo.is_connected("user-a", "gmail") is True
    assert await reader.repo.is_connected("user-b", "microsoft_todo") is False
    assert sorted(await reader.repo.list_connected_providers("user-a")) == ["gmail", "microsoft_todo"]
    assert sorted(await reader.repo.list_users_by_provider("gmail")) == ["user-a", "user-b"]
    assert await reader.repo.list_users_by_provider("microsoft_onedrive") == []


async def test_a_read_never_returns_another_users_or_kinds_record(store):
    await store.repo.save_credentials(creds("user-a", "gmail"))
    assert await store.repo.get_credentials("user-b", "gmail") is None
    assert await store.repo.get_credentials("user-a", "google_tasks") is None


async def test_revoke_deletes_only_that_kind_and_is_idempotent(store):
    await store.repo.save_credentials(creds("user-a", "gmail"))
    await store.repo.save_credentials(creds("user-a", "google_tasks"))
    await store.repo.revoke_credentials("user-a", "gmail")
    await store.repo.revoke_credentials("user-a", "gmail")
    assert [d["kind"] for d in store.docs.values()] == ["google_tasks"]
    assert await store.repo.get_credentials("user-a", "gmail") is None


async def test_two_records_of_one_kind_raise_instead_of_guessing(store):
    await store.repo.save_credentials(creds())
    (doc_id, doc), = store.docs.items()
    store.docs["user-a:another"] = dict(doc, credential_id="another")
    with pytest.raises(RuntimeError):
        await store.repo.get_credentials("user-a", "gmail")
