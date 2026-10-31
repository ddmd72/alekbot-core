"""
FirestoreCredentialStore — the integration credentials at rest (MULTI_DRIVE_RFC §4.1, §4.2; SECRETS_AT_REST_RFC §2.3,
§2.4; invariants I1-I4, I9).

Collection `{prefix}credentials`, doc id `{user_id}:{credential_id}`. `credential_id` is opaque, random and immutable:
it survives every refresh and every consent of the same account (I5). Plaintext fields are the ones queried or
operational: `user_id`, `credential_id`, `kind`, `integration`, `account_id`, `account.realm`, `account.subject`,
`scopes`, `token_expiry`, `needs_reconnect`, `created_at` and `updated_at`. `secret`
is ONE `CipherPort` ciphertext of `{access_token, refresh_token, account_label}`, AAD `credentials/{user_id}:
{credential_id}`, so a ciphertext moved to another record does not decrypt (I2) and a read or write is one cipher call.

- Any number of records per (user, kind): cardinality is the connect service's policy (E15), not the store's.
- `save` (a consent): refuses a credential without its account subject, `integration` or `account_id` and writes
  nothing (I9). One transaction: the same (user, kind, account realm + subject) keeps its `credential_id` and
  `created_at`; any other consent gets a new record and deletes none.
- `update` (a refresh or a flag write): a transaction on the one document, exists-or-False. It keeps the stored
  `credential_id`, `kind`, `integration`, `account_id`, `account.realm`, `account.subject` and never takes them from
  its argument (plan D-P2).
- A record written before `integration` / `account_id` existed reads them as None, never derived here: the backfill
  sets them.
- `list_keys`, `list_by_account` and `list_user_ids` read plaintext fields only: no cipher call (an existence check
  never decrypts, so a cipher failure is never "not connected").
- A decrypt or cipher failure raises (I4); it is never "not connected".
"""

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.credential_key import CredentialKey
from ..domain.integration import Integration
from ..domain.oauth_credential import OAuthCredential
from ..domain.provider_account import ProviderAccount
from ..ports.cipher_port import CipherPort
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..utils.logger import logger

_AAD_STORE = "credentials"


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

    def _of_user(self, user_id: str, kind: Optional[str] = None) -> Any:
        query = self._col.where(filter=FieldFilter("user_id", "==", user_id))
        return query if kind is None else query.where(filter=FieldFilter("kind", "==", kind))

    async def _seal(self, doc_id: str, credential: OAuthCredential) -> str:
        payload = json.dumps({"access_token": credential.access_token, "refresh_token": credential.refresh_token,
                              "account_label": credential.account.label})
        return await self._cipher.encrypt(payload, aad=self._aad(doc_id))

    @staticmethod
    def _plain_fields(credential: OAuthCredential, kind: str, now: datetime) -> Dict[str, Any]:
        """The mutable plaintext fields a consent or an update writes (identity and created_at excluded). `kind` is the
        record's stored kind on an update, never the argument's."""
        fields: Dict[str, Any] = {
            "scopes": list(credential.scopes or []),
            "token_expiry": credential.token_expiry,
            "needs_reconnect": bool(credential.needs_reconnect),
            "updated_at": now,
        }
        return fields

    async def _open(self, snap: Any) -> OAuthCredential:
        data = snap.to_dict()
        try:
            payload = json.loads(await self._cipher.decrypt(data["secret"], aad=self._aad(snap.id)))
        except Exception as exc:
            logger.error(f"💥 Credential decrypt failed: user={str(data.get('user_id'))[:8]} kind={data.get('kind')} "
                         f"credential={str(data.get('credential_id'))[:8]}: {type(exc).__name__}")
            raise
        account = data["account"]
        integration = data.get("integration")
        return OAuthCredential(
            credential_id=data["credential_id"],
            user_id=data["user_id"],
            account_id=data.get("account_id"),
            kind=data["kind"],
            integration=None if integration is None else Integration(integration),
            account=ProviderAccount(realm=account["realm"], subject=account["subject"],
                                    label=payload["account_label"]),
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            token_expiry=_utc(data["token_expiry"]),
            scopes=list(data["scopes"]),
            needs_reconnect=bool(data["needs_reconnect"]),
        )

    @staticmethod
    def _created(snap: Any) -> Any:
        created = (snap.to_dict() or {}).get("created_at")
        return created if created is not None else datetime.min.replace(tzinfo=timezone.utc)

    # ------------------------------------------------------------------ port

    async def get(self, user_id: str, credential_id: str) -> Optional[OAuthCredential]:
        if not credential_id:
            return None
        snap = await self._col.document(self._doc_id(user_id, credential_id)).get()
        if not snap.exists:
            return None
        if (snap.to_dict() or {}).get("user_id") != user_id:
            # the doc id names the user; a record whose stored user differs is a broken invariant, never served
            logger.error(f"💥 Credential {credential_id[:8]} stored under another user; not returned")
            return None
        return await self._open(snap)

    async def save(self, credential: OAuthCredential) -> str:
        user_id, kind = credential.user_id, credential.kind
        missing = [name for name, value in (("account subject", (credential.account.subject or "").strip()),
                                            ("integration", credential.integration),
                                            ("account_id", (credential.account_id or "").strip()))
                   if not value]
        if missing:
            logger.error(f"💥 Credential not saved: no {', '.join(missing)} (user={user_id[:8]} kind={kind})")
            raise ValueError(f"A credential needs its {', '.join(missing)}; nothing was stored")
        integration = credential.integration
        if integration is None:   # refused above; for the type checker
            raise ValueError("A credential needs its integration; nothing was stored")
        realm, subject = credential.account.realm, credential.account.subject
        query = self._of_user(user_id, kind)

        @firestore.async_transactional
        async def _save(transaction: Any) -> str:
            rows = [s for s in await query.get(transaction=transaction) if s.exists]
            same = [s for s in rows
                    if (s.to_dict().get("account") or {}) == {"realm": realm, "subject": subject}]
            kept = same[0].to_dict() if same else None
            credential_id = kept["credential_id"] if kept else secrets.token_hex(16)
            doc_id = self._doc_id(user_id, credential_id)
            now = datetime.now(timezone.utc)
            sealed = await self._seal(doc_id, credential)
            doc = {
                "user_id": user_id,
                "credential_id": credential_id,
                "kind": kind,
                "integration": integration.value,
                "account_id": credential.account_id,
                "account": {"realm": realm, "subject": subject},
                "created_at": kept["created_at"] if kept else now,
                "secret": sealed,
                **self._plain_fields(credential, kind, now),
            }
            transaction.set(self._col.document(doc_id), doc)
            return credential_id

        credential_id = await _save(self._db.transaction())
        logger.info(f"💾 Credential saved: user={user_id[:8]} kind={kind} credential={credential_id[:8]}")
        return credential_id

    async def update(self, credential: OAuthCredential) -> bool:
        user_id, credential_id = credential.user_id, credential.credential_id
        if not credential_id:
            logger.error(f"💥 Credential update without a credential_id (user={user_id[:8]} kind={credential.kind})")
            return False
        doc_id = self._doc_id(user_id, credential_id)
        ref = self._col.document(doc_id)

        @firestore.async_transactional
        async def _update(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            stored = snap.to_dict() if snap.exists else None
            if stored is None or stored.get("user_id") != user_id:
                return False
            sealed = await self._seal(doc_id, credential)
            transaction.update(ref, {"secret": sealed,
                                     **self._plain_fields(credential, stored["kind"], datetime.now(timezone.utc))})
            return True

        if not await _update(self._db.transaction()):
            logger.info(f"Credential not updated, record gone: user={user_id[:8]} credential={credential_id[:8]}")
            return False
        logger.debug(f"💾 Credential updated: user={user_id[:8]} credential={credential_id[:8]}")
        return True

    async def delete(self, user_id: str, credential_id: str) -> None:
        if not credential_id:
            return
        await self._col.document(self._doc_id(user_id, credential_id)).delete()
        logger.info(f"🗑️ Credential deleted: user={user_id[:8]} credential={credential_id[:8]}")

    async def list_for_user(self, user_id: str, kind: Optional[str] = None) -> List[OAuthCredential]:
        snaps = sorted((s for s in await self._of_user(user_id, kind).get() if s.exists), key=self._created)
        return [await self._open(snap) for snap in snaps]

    async def list_keys(self, user_id: str, kind: Optional[str] = None) -> List[CredentialKey]:
        snaps = sorted((s for s in await self._of_user(user_id, kind).get() if s.exists), key=self._created)
        return [CredentialKey(user_id=user_id, credential_id=(s.to_dict() or {})["credential_id"]) for s in snaps]

    async def list_by_account(self, realm: str, subject: str) -> List[CredentialKey]:
        snaps = await (self._col.where(filter=FieldFilter("account.realm", "==", realm))
                       .where(filter=FieldFilter("account.subject", "==", subject)).get())
        keys: List[CredentialKey] = []
        for snap in snaps:
            data = snap.to_dict() or {}
            if snap.exists and data.get("user_id") and data.get("credential_id"):
                keys.append(CredentialKey(user_id=data["user_id"], credential_id=data["credential_id"]))
        return keys

    async def list_user_ids(self, kind: str) -> List[str]:
        snaps = await self._col.where(filter=FieldFilter("kind", "==", kind)).get()
        users: List[str] = []
        for snap in snaps:
            user_id = (snap.to_dict() or {}).get("user_id")
            if user_id and user_id not in users:
                users.append(user_id)
        return users
