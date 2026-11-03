"""Mutation survivor of `src/adapters/firestore_credential_store.py` (p3 · g6): the same-account lookup inside `save`
reads through its transaction, so a concurrent commit to the found document aborts and re-runs the body (a read outside
the transaction would be lost-update prone)."""
from types import SimpleNamespace

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.composition.connector_registry import ConnectorRegistry
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb

CREDS = "eval_credentials"


class CountingCipher(AesGcmTestCipher):
    def __init__(self) -> None:
        super().__init__()
        self.encrypts = 0

    async def encrypt(self, plaintext: str, *, aad: str) -> str:
        self.encrypts += 1
        return await super().encrypt(plaintext, aad=aad)


def _cred() -> OAuthCredential:
    return OAuthCredential(user_id="usr-1", account_id="acc-1", kind="gmail",
                           integration=ConnectorRegistry().get("gmail").integration,
                           account=ProviderAccount(realm="google", subject="sub-1", label="user@example.com"),
                           access_token="at-1", refresh_token="rt-1", token_expiry=None, scopes=["s.read"])


async def test_save_reruns_when_the_found_document_is_changed_by_a_concurrent_transaction():
    db = EvalDb()
    cipher = CountingCipher()
    repo = FirestoreCredentialStore(db, SimpleNamespace(credentials_collection=CREDS), cipher=cipher)
    credential_id = await repo.save(_cred())
    per_run = cipher.encrypts   # one body run seals each secret once
    assert per_run > 0
    doc_id = next(iter(db.collection(CREDS).docs))
    db.interfere_on_txn_query(CREDS, doc_id, {"touched": True})
    assert await repo.save(_cred()) == credential_id
    assert cipher.encrypts == 3 * per_run   # the second save ran its body twice: the read was part of the transaction
