"""
OAuthCredentialsPort — persist OAuth tokens, provider-agnostic.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.email import OAuthCredentials


class OAuthCredentialsPort(ABC):
    """
    Port for stored OAuth grants: at most one record per (user_id, provider) (SECRETS_AT_REST_RFC §2.4, I5);
    providers ("kinds") gmail, google_tasks, microsoft_todo, microsoft_onedrive.

    Holds for every method:
    - Tenancy: a read for one (user_id, provider) never returns another user's or another provider's record; the
      adapter finds the record by an equality query on its stored user and kind.
    - Identity: every record carries its account identity (account_realm, account_subject) and an opaque
      credential_id from its first write (I9). An update keeps the stored identity and never takes it from its
      argument; a consent of the same account keeps the credential_id, another account replaces the record.
    - Expiry: no method checks or refreshes token_expiry; the caller refreshes and updates.
    - Atomicity: each write is one backend transaction; between two updates the last writer wins. Callers set
      needs_reconnect by get + update_credentials and accept the race between two updates
      (decisions/gmail_grant_revoked_reconnect.md); an update never re-creates a record revoked meanwhile (see
      update_credentials).
    - Errors: backend and decrypt errors propagate; no method turns an error into "connected" or "not connected".
    """

    @abstractmethod
    async def get_credentials(
        self, user_id: str, provider: str
    ) -> Optional[OAuthCredentials]:
        """Return the record of (user_id, provider), or None when there is none (not connected).

        Returns every OAuthCredentials field as saved, token_expiry tz-aware UTC, so get → update keeps them all
            (worker_handler.py:1019-1025, onedrive_adapter.py:542-545). A record with needs_reconnect True is
            returned; the caller decides.
        """

    @abstractmethod
    async def save_credentials(self, credentials: OAuthCredentials) -> None:
        """Store `credentials` as the record of (credentials.user_id, credentials.provider): the consent path.

        Upsert: creates the record or replaces the whole one, so a save after a new consent clears needs_reconnect.
            The same account (realm + subject) keeps its credential_id; another account gets a new one and the old
            record is deleted. Raises, storing nothing, when credentials.account_subject is empty (I9).
            Only a consent (oauth_app.py) calls it; a refresh or a flag write goes through update_credentials, so it
            cannot bring back a record that revoke_credentials deleted.
        """

    @abstractmethod
    async def update_credentials(self, credentials: OAuthCredentials) -> bool:
        """Replace the record of (credentials.user_id, credentials.provider) only if it still exists: the path for a
        token refresh and for the needs_reconnect flag.

        Returns True when the record was replaced (tokens, label, scopes, expiry, needs_reconnect and drive_id from
            `credentials`, so a refresh clears needs_reconnect; credential_id, kind and account identity stay as
            stored), False when there is none: nothing is written, and the caller treats the user as not
            connected. The existence check and the write are one atomic backend operation, not a read then a write,
            so a disconnect that lands first stands.
        """

    @abstractmethod
    async def revoke_credentials(self, user_id: str, provider: str) -> None:
        """Delete the record of (user_id, provider): get_credentials then returns None, is_connected False, and
        list_users_by_provider omits the user.

        Provider-side revocation is not part of this method; Gmail and Google Tasks disconnects revoke first, OneDrive
            and To Do do not (onedrive_adapter.py:247, task_setup_service.py:233).

        Idempotency: an absent record is a no-op, no error.
        Tenancy: the user's records for other providers stay.
        """

    @abstractmethod
    async def is_connected(self, user_id: str, provider: str) -> bool:
        """True exactly when a record of (user_id, provider) exists, including one get_credentials raises on;
        needs_reconnect and token_expiry are not considered."""

    @abstractmethod
    async def list_connected_providers(self, user_id: str) -> List[str]:
        """Return the provider of every record whose stored user_id is `user_id`.

        Contract (observed; no caller in src/). Order: none.
        """

    @abstractmethod
    async def list_users_by_provider(self, provider: str) -> List[str]:
        """Return the user_id of every record of `provider`, each user once, needs_reconnect ones included.

        Order: none; callers iterate. Limit: none.
        """
