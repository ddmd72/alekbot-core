"""The real FirestoreCredentialStore over the evaluating Firestore fake and the AES-GCM test cipher, for tests of code
that refreshes or flags a grant. `disconnect_after_read` makes the race of a disconnect landing while a refresh is in
flight. `creds()` carries a synthetic account identity, integration and account_id (SECRETS_AT_REST_RFC §2.3, I9;
MULTI_DRIVE_RFC §4.2): the store refuses a credential without them. `one()` reads the user's one credential of a kind,
the way the single-credential consumers find theirs."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.composition.connector_registry import ConnectorRegistry
from src.domain.credential_key import CredentialKey
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[2] / "config" / "firestore.indexes.json"
COLLECTION = "eval_credentials"
EXPIRY = datetime(2026, 10, 10, 11, 12, 13, tzinfo=timezone.utc)
ACCOUNT_ID = "acct-1"
_REGISTRY = ConnectorRegistry()
_KINDS = ("gmail", "google_tasks", "microsoft_todo", "microsoft_onedrive")


async def one(repo: Any, user_id: str, kind: str) -> Optional[OAuthCredential]:
    """The user's credential of `kind` (the oldest when several), or None."""
    found = await repo.list_for_user(user_id, kind)
    return found[0] if found else None


class CredentialsStore:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={COLLECTION: "development_credentials"})
        self.cipher = AesGcmTestCipher()
        self.repo = FirestoreCredentialStore(self.db, SimpleNamespace(credentials_collection=COLLECTION),
                                             cipher=self.cipher)

    @property
    def docs(self) -> dict:
        return self.db.collection(COLLECTION).docs

    def doc_of(self, user_id: str, kind: str) -> Optional[dict]:
        """The one stored document of (user_id, kind), or None (doc ids are opaque: `{user_id}:{credential_id}`)."""
        found = [d for d in self.docs.values() if d.get("user_id") == user_id and d.get("kind") == kind]
        assert len(found) <= 1, f"more than one record of {kind}"
        return found[0] if found else None

    def disconnect_after_read(self, user_id: str, kind: str) -> None:
        """The next read of the user's credential of `kind` (`list_for_user` or `get`) returns the record and then the
        user disconnects: what a refresh or flag write that started from that read finds when it writes."""
        real_list, real_get = self.repo.list_for_user, self.repo.get

        async def disconnect() -> None:
            for credential in await real_list(user_id, kind):
                await self.repo.delete(user_id, credential.credential_id)
            self.repo.list_for_user, self.repo.get = real_list, real_get

        async def list_then_disconnect(uid, of_kind=None):
            got = await real_list(uid, of_kind)
            if uid == user_id and any(c.kind == kind for c in got):
                await disconnect()
            return got

        async def get_then_disconnect(uid, credential_id):
            got = await real_get(uid, credential_id)
            if got is not None and uid == user_id and got.kind == kind:
                await disconnect()
            return got

        self.repo.list_for_user, self.repo.get = list_then_disconnect, get_then_disconnect


def creds(user_id: str, kind: str, **over) -> OAuthCredential:
    spec = _REGISTRY.get(kind)
    fields = dict(user_id=user_id, account_id=ACCOUNT_ID, kind=kind, integration=spec.integration,
                  account=ProviderAccount(realm=spec.realm, subject=f"subject-{kind}", label="owner@example.com"),
                  access_token="at-old", refresh_token="rt-old", token_expiry=EXPIRY, scopes=["s.read"],
                  needs_reconnect=False, drive_id="")
    fields.update(over)
    return OAuthCredential(**fields)


async def delete_kind(repo: Any, user_id: str, kind: str) -> None:
    """Delete every credential of the user of `kind` (what a disconnect of that integration does; no decrypt)."""
    for key in await repo.list_keys(user_id, kind):
        await repo.delete(user_id, key.credential_id)


async def has_kind(repo: Any, user_id: str, kind: str) -> bool:
    """True when the user has a credential of `kind` (the existence check: plaintext only, no decrypt)."""
    return bool(await repo.list_keys(user_id, kind))


async def kinds_of(repo: Any, user_id: str) -> list:
    """The kind of every credential of the user, one entry per record (plaintext only, no decrypt)."""
    return [kind for kind in _KINDS for _ in await repo.list_keys(user_id, kind)]


def mirror_get(port: Any) -> None:
    """Script a mock credential port whose one stored credential is `port.get.return_value` (None: none stored):
    `list_keys` and `list_for_user` answer from it at each call, so the three reads agree as the store's do."""
    def held() -> list:
        credential = port.get.return_value
        return [] if credential is None else [credential]

    port.list_keys = AsyncMock(side_effect=lambda *a, **k: [
        CredentialKey(user_id=c.user_id, credential_id=c.credential_id) for c in held()])
    port.list_for_user = AsyncMock(side_effect=lambda *a, **k: held())
