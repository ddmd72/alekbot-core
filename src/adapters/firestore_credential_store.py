"""
FirestoreCredentialStore — the integration credentials at rest (SECRETS_AT_REST_RFC §2.3, §2.4; invariants I1-I5, I9).

Collection `{prefix}credentials`, doc id `{user_id}:{credential_id}`. `credential_id` is opaque, random and immutable:
it survives every refresh and every consent of the same account (I5). Plaintext fields are the ones queried or
operational: `user_id`, `credential_id`, `kind`, `account.realm`, `account.subject`, `scopes`, `token_expiry`,
`needs_reconnect`, `created_at`, `updated_at`, and `drive_id` on a OneDrive record. `secret` is ONE `CipherPort`
ciphertext of `{access_token, refresh_token, account_label}`, AAD `credentials/{user_id}:{credential_id}`, so a
ciphertext moved to another record does not decrypt (I2) and a read or write is one cipher call.

- At most one record per (user, kind) (stage 0), found by an equality query on `user_id` + `kind`.
- `save_credentials` (a consent): refuses a credential without `account_subject` and writes nothing (I9). One
  transaction: the same account (realm + subject) keeps its `credential_id`; another account gets a new id and the old
  record is deleted.
- `update_credentials` (a refresh or a flag write): a transaction, exists-or-False. It keeps the stored `credential_id`,
  `kind`, `account.realm`, `account.subject` and never takes them from its argument (plan D-P2): every refresh rebuilds
  the credential field by field.
- `is_connected` and the two listings read plaintext fields only: no cipher call.
- A decrypt or cipher failure raises (I4); it is never "not connected".

No legacy branch (§2.7): the old `oauth_credentials` collection is never read or written here.
"""

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from google.api_core.exceptions import NotFound
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.email import OAuthCredentials
from ..ports.cipher_port import CipherPort
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..utils.logger import logger

_AAD_STORE = "credentials"
_ONEDRIVE_KIND = "microsoft_onedrive"


def _utc(raw: Any) -> datetime:
    # Always construct a plain datetime: DatetimeWithNanoseconds.replace(tzinfo=None) bypasses __init__ and leaves
    # ._nanosecond unset, which breaks a later write-back to Firestore.
    return datetime(raw.year, raw.month, raw.day, raw.hour, raw.minute, raw.second, raw.microsecond,
                    tzinfo=timezone.utc)


class FirestoreCredentialStore(OAuthCredentialsPort):

    def __init__(self, db_client: Any, env_config: EnvironmentConfig, *, cipher: CipherPort):
        self._db = db_client
        self._cipher = cipher
        name = env_config.credentials_collection
        self._col = db_client.collection(name)
        logger.info(f"📂 Credential store initialized. Collection: {name}")

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _doc_id(user_id: str, credential_id: str) -> str:
        return f"{user_id}:{credential_id}"

    @staticmethod
    def _aad(doc_id: str) -> str:
        return f"{_AAD_STORE}/{doc_id}"

    def _of_kind(self, user_id: str, kind: str) -> Any:
        return (self._col.where(filter=FieldFilter("user_id", "==", user_id))
                .where(filter=FieldFilter("kind", "==", kind)))

    async def _seal(self, doc_id: str, credentials: OAuthCredentials) -> str:
        payload = json.dumps({"access_token": credentials.access_token, "refresh_token": credentials.refresh_token,
                              "account_label": credentials.email_address})
        return await self._cipher.encrypt(payload, aad=self._aad(doc_id))

    @staticmethod
    def _plain_fields(credentials: OAuthCredentials, now: datetime) -> Dict[str, Any]:
        """The mutable plaintext fields a consent or an update writes (identity and created_at excluded)."""
        fields: Dict[str, Any] = {
            "scopes": list(credentials.scopes or []),
            "token_expiry": credentials.token_expiry,
            "needs_reconnect": bool(credentials.needs_reconnect),
            "updated_at": now,
        }
        if credentials.provider == _ONEDRIVE_KIND:
            fields["drive_id"] = credentials.drive_id or ""
        return fields

    @staticmethod
    def _single(snaps: List[Any], user_id: str, kind: str) -> Optional[Any]:
        found = [s for s in snaps if s.exists]
        if len(found) > 1:
            # stage 0 holds at most one record per (user, kind) (I5): two is a broken invariant, never a guess
            logger.error(f"💥 {len(found)} credential records for user={user_id[:8]} kind={kind}; expected at most one")
            raise RuntimeError(f"More than one credential record for kind {kind}")
        return found[0] if found else None

    async def _open(self, snap: Any) -> OAuthCredentials:
        data = snap.to_dict()
        try:
            payload = json.loads(await self._cipher.decrypt(data["secret"], aad=self._aad(snap.id)))
        except Exception as exc:
            logger.error(f"💥 Credential decrypt failed: user={str(data.get('user_id'))[:8]} kind={data.get('kind')} "
                         f"credential={str(data.get('credential_id'))[:8]}: {type(exc).__name__}")
            raise
        account = data["account"]
        return OAuthCredentials(
            user_id=data["user_id"],
            provider=data["kind"],
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            token_expiry=_utc(data["token_expiry"]),
            scopes=list(data["scopes"]),
            email_address=payload["account_label"],
            needs_reconnect=bool(data["needs_reconnect"]),
            drive_id=str(data.get("drive_id") or ""),
            credential_id=data["credential_id"],
            account_realm=account["realm"],
            account_subject=account["subject"],
        )

    # ------------------------------------------------------------------ port

    async def get_credentials(self, user_id: str, provider: str) -> Optional[OAuthCredentials]:
        snap = self._single(await self._of_kind(user_id, provider).get(), user_id, provider)
        return None if snap is None else await self._open(snap)

    async def save_credentials(self, credentials: OAuthCredentials) -> None:
        user_id, kind = credentials.user_id, credentials.provider
        if not (credentials.account_subject or "").strip():
            logger.error(f"💥 Credential not saved: no account identity (user={user_id[:8]} kind={kind})")
            raise ValueError("A credential needs its account identity (account_subject); nothing was stored")
        query = self._of_kind(user_id, kind)

        @firestore.async_transactional
        async def _save(transaction: Any) -> str:
            rows = [s for s in await query.get(transaction=transaction) if s.exists]
            same = [s for s in rows
                    if (s.to_dict().get("account") or {}) == {"realm": credentials.account_realm,
                                                             "subject": credentials.account_subject}]
            kept = same[0].to_dict() if same else None
            credential_id = kept["credential_id"] if kept else secrets.token_hex(16)
            doc_id = self._doc_id(user_id, credential_id)
            now = datetime.now(timezone.utc)
            sealed = await self._seal(doc_id, credentials)
            doc = {
                "user_id": user_id,
                "credential_id": credential_id,
                "kind": kind,
                "account": {"realm": credentials.account_realm, "subject": credentials.account_subject},
                "created_at": kept["created_at"] if kept else now,
                "secret": sealed,
                **self._plain_fields(credentials, now),
            }
            transaction.set(self._col.document(doc_id), doc)
            for snap in rows:
                if snap.id != doc_id:
                    transaction.delete(snap.reference)
            return credential_id

        credential_id = await _save(self._db.transaction())
        logger.info(f"💾 Credential saved: user={user_id[:8]} kind={kind} credential={credential_id[:8]}")

    async def update_credentials(self, credentials: OAuthCredentials) -> bool:
        user_id, kind = credentials.user_id, credentials.provider
        query = self._of_kind(user_id, kind)

        @firestore.async_transactional
        async def _update(transaction: Any) -> Optional[str]:
            snap = self._single(await query.get(transaction=transaction), user_id, kind)
            if snap is None:
                return None
            sealed = await self._seal(snap.id, credentials)
            transaction.update(snap.reference, {"secret": sealed,
                                                **self._plain_fields(credentials, datetime.now(timezone.utc))})
            return snap.id

        try:
            doc_id = await _update(self._db.transaction())
        except NotFound:
            doc_id = None
        if doc_id is None:
            logger.info(f"Credential not updated, record gone: user={user_id[:8]} kind={kind}")
            return False
        logger.debug(f"💾 Credential updated: user={user_id[:8]} kind={kind}")
        return True

    async def revoke_credentials(self, user_id: str, provider: str) -> None:
        for snap in await self._of_kind(user_id, provider).get():
            await snap.reference.delete()
        logger.info(f"🗑️ Credential deleted: user={user_id[:8]} kind={provider}")

    async def is_connected(self, user_id: str, provider: str) -> bool:
        return any(s.exists for s in await self._of_kind(user_id, provider).get())

    async def list_connected_providers(self, user_id: str) -> List[str]:
        snaps = await self._col.where(filter=FieldFilter("user_id", "==", user_id)).get()
        return [s.to_dict()["kind"] for s in snaps if s.exists]

    async def list_users_by_provider(self, provider: str) -> List[str]:
        snaps = await self._col.where(filter=FieldFilter("kind", "==", provider)).get()
        users: List[str] = []
        for snap in snaps:
            user_id = (snap.to_dict() or {}).get("user_id")
            if user_id and user_id not in users:
                users.append(user_id)
        return users
