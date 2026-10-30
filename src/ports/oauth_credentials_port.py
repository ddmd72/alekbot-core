"""
OAuthCredentialsPort — the stored OAuth grants, keyed by credential (MULTI_DRIVE_RFC §4.2; SECRETS_AT_REST_RFC §2.3).
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.credential_key import CredentialKey
from src.domain.oauth_credential import OAuthCredential


class OAuthCredentialsPort(ABC):
    """
    Port for stored OAuth grants. A record is found by (user_id, credential_id); no method takes a provider
    argument, and a `kind` reaching it comes from an injected `ConnectorSpec`, never a literal (T-6).

    Holds for every method:
    - Cardinality: the store holds any number of records per (user_id, kind). How many a kind may have is product
      policy (`ConnectorSpec.multiple`), decided above the port (E15).
    - Tenancy: a read for one user never returns another user's record (the doc id is `{user_id}:{credential_id}`
      and the stored user is checked); only `list_by_account` and `list_user_ids` read across users.
    - Identity: every record carries `kind`, `integration`, `account_id`, its account (realm + subject) and an opaque
      `credential_id` from its first write (I9). An update keeps the stored identity and never takes it from its
      argument (D-P2). A record written before `integration` / `account_id` existed reads them as None.
    - Expiry: no method checks or refreshes token_expiry; the caller refreshes and updates.
    - Atomicity: each write is one backend transaction; between two updates the last writer wins; an update never
      re-creates a record deleted meanwhile.
    - Errors: backend and decrypt errors propagate; no method turns an error into "connected" or "not connected".
    """

    @abstractmethod
    async def get(self, user_id: str, credential_id: str) -> Optional[OAuthCredential]:
        """The record of (user_id, credential_id) with every field as saved (token_expiry tz-aware UTC), or None when
        there is none. A record with needs_reconnect True is returned; the caller decides."""

    @abstractmethod
    async def save(self, credential: OAuthCredential) -> None:
        """Store `credential` (the consent path): create, or replace the whole record of the same account.

        The store chooses `credential_id` on the first save; the argument's `credential_id` is never used. A save of
        the same (user, kind, account realm + subject) keeps its record's id and `created_at` and rewrites the rest, so
        a new consent clears needs_reconnect. A save of another account creates another record and deletes none.
        Raises, storing nothing, when the account subject, `integration` or `account_id` is missing (I9).
        """

    @abstractmethod
    async def update(self, credential: OAuthCredential) -> bool:
        """Replace the record of (credential.user_id, credential.credential_id) only if it still exists: a token
        refresh or the needs_reconnect flag.

        Returns True when replaced (tokens, label, scopes, expiry, needs_reconnect and drive_id from `credential`;
        credential_id, kind, integration, account_id and the account realm + subject stay as stored), False when the
        record is gone: nothing is written. The existence check and the write are one atomic backend operation.
        """

    @abstractmethod
    async def delete(self, user_id: str, credential_id: str) -> None:
        """Delete the record of (user_id, credential_id). Provider-side revocation is not part of it.
        An absent record is a no-op; the user's other records stay."""

    @abstractmethod
    async def list_for_user(self, user_id: str, kind: Optional[str] = None) -> List[OAuthCredential]:
        """Every record of the user, or only those of `kind`. Order: oldest first (`created_at`)."""

    @abstractmethod
    async def list_by_account(self, realm: str, subject: str) -> List[CredentialKey]:
        """The key of every record of the provider account (realm + subject), across users (the revoke rule, §4.3).
        Reads plaintext fields only. Order: none."""

    @abstractmethod
    async def list_user_ids(self, kind: str) -> List[str]:
        """The user_id of every user with a record of `kind`, each once, needs_reconnect ones included.
        Reads plaintext fields only. Order: none; limit: none."""
