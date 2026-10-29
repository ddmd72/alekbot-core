"""The real FirestoreCredentialStore over the evaluating Firestore fake and the AES-GCM test cipher, for tests of code
that refreshes or flags a grant. `disconnect_after_read` makes the race of a disconnect landing while a refresh is in
flight. `creds()` carries a synthetic account identity (SECRETS_AT_REST_RFC §2.3, I9): the store refuses a credential
without one."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.domain.email import OAuthCredentials
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[2] / "config" / "firestore.indexes.json"
COLLECTION = "eval_credentials"
EXPIRY = datetime(2026, 10, 10, 11, 12, 13, tzinfo=timezone.utc)
_REALM = {"gmail": "google", "google_tasks": "google", "microsoft_todo": "microsoft", "microsoft_onedrive": "microsoft"}


class CredentialsStore:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={COLLECTION: "development_credentials"})
        self.cipher = AesGcmTestCipher()
        self.repo = FirestoreCredentialStore(self.db, SimpleNamespace(credentials_collection=COLLECTION),
                                             cipher=self.cipher)

    @property
    def docs(self) -> dict:
        return self.db.collection(COLLECTION).docs

    def doc_of(self, user_id: str, provider: str) -> Optional[dict]:
        """The one stored document of (user_id, provider), or None (doc ids are opaque: `{user_id}:{credential_id}`)."""
        found = [d for d in self.docs.values() if d.get("user_id") == user_id and d.get("kind") == provider]
        assert len(found) <= 1, f"more than one record of {provider}"
        return found[0] if found else None

    def disconnect_after_read(self, user_id: str, provider: str) -> None:
        """The next get_credentials of (user_id, provider) returns the record and then the user disconnects: what a
        refresh or flag write that started from that read finds when it writes."""
        real_get = self.repo.get_credentials

        async def get_then_disconnect(uid, prov):
            got = await real_get(uid, prov)
            if (uid, prov) == (user_id, provider):
                await self.repo.revoke_credentials(user_id, provider)
                self.repo.get_credentials = real_get
            return got

        self.repo.get_credentials = get_then_disconnect


def creds(user_id: str, provider: str, **over) -> OAuthCredentials:
    fields = dict(user_id=user_id, provider=provider, access_token="at-old", refresh_token="rt-old",
                  token_expiry=EXPIRY, scopes=["s.read"], email_address="owner@example.com",
                  needs_reconnect=False, drive_id="", account_realm=_REALM.get(provider, "google"),
                  account_subject=f"subject-{provider}")
    fields.update(over)
    return OAuthCredentials(**fields)
