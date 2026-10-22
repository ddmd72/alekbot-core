"""The real FirestoreOAuthCredentialsAdapter over the evaluating Firestore fake, for tests of code that refreshes or
flags a grant. `disconnect_after_read` makes the race of a disconnect landing while a refresh is in flight."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.adapters.firestore_oauth_credentials_adapter import FirestoreOAuthCredentialsAdapter
from src.domain.email import OAuthCredentials
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[2] / "config" / "firestore.indexes.json"
COLLECTION = "eval_oauth_credentials"
EXPIRY = datetime(2026, 10, 10, 11, 12, 13, tzinfo=timezone.utc)


class CredentialsStore:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={COLLECTION: "development_oauth_credentials"})
        self.repo = FirestoreOAuthCredentialsAdapter(self.db, SimpleNamespace(oauth_credentials_collection=COLLECTION))

    @property
    def docs(self) -> dict:
        return self.db.collection(COLLECTION).docs

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
                  needs_reconnect=False, drive_id="")
    fields.update(over)
    return OAuthCredentials(**fields)
