"""
OAuthCredentialsPort — persist OAuth tokens, provider-agnostic.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.email import OAuthCredentials


class OAuthCredentialsPort(ABC):
    """
    Port for stored OAuth grants: one record per (user_id, provider); providers gmail, google_tasks,
    microsoft_todo, microsoft_onedrive.

    Holds for every method:
    - Tenancy: a read for one (user_id, provider) never returns another user's or another provider's record. The
      adapter keys `{user_id}_{provider}` without comparing the stored fields; unambiguous because user ids are
      uuid4, with no `_` (observed).
    - Expiry: no method checks or refreshes token_expiry; the caller refreshes and saves.
    - Legacy documents: a missing needs_reconnect reads as False, a missing or null drive_id as "". A document
      missing any other field raises in get_credentials (observed).
    - Atomicity: one document per write, no compare-and-set, last writer wins. Callers set needs_reconnect by get
      + save and accept that race (decisions/gmail_grant_revoked_reconnect.md).
    - Errors: backend errors propagate; no method turns an error into "connected" or "not connected".
    """

    @abstractmethod
    async def get_credentials(
        self, user_id: str, provider: str
    ) -> Optional[OAuthCredentials]:
        """Return the record of (user_id, provider), or None when there is none (not connected).

        Returns every OAuthCredentials field as saved, token_expiry tz-aware UTC, so get → save keeps them all
            (worker_handler.py:1019-1025, onedrive_adapter.py:542-545). A record with needs_reconnect True is
            returned; the caller decides.
        """

    @abstractmethod
    async def save_credentials(self, credentials: OAuthCredentials) -> None:
        """Store `credentials` as the record of (credentials.user_id, credentials.provider).

        Idempotency: replaces the whole record, so a save after a new consent or a successful refresh clears
            needs_reconnect.
        Known defect: callers of revoke_credentials need a disconnect to stand, but a save is an upsert, so a token
            refresh in flight during a disconnect writes the grant back (graph_auth.py:123 against
            onedrive_adapter.py:247; also email_review_service.py:205, email_search_service.py:288,
            google_tasks_adapter.py:235).
        """

    @abstractmethod
    async def revoke_credentials(self, user_id: str, provider: str) -> None:
        """Delete the record of (user_id, provider): get_credentials then returns None, is_connected False, and
        list_users_by_provider omits the user. Revoking the grant at the provider is the caller's job, done first.

        Idempotency: an absent record is a no-op, no error.
        Tenancy: the user's records for other providers stay.
        """

    @abstractmethod
    async def is_connected(self, user_id: str, provider: str) -> bool:
        """True exactly when get_credentials returns a record; needs_reconnect and token_expiry are not considered."""

    @abstractmethod
    async def list_connected_providers(self, user_id: str) -> List[str]:
        """Return the provider of every record whose stored user_id is `user_id`.

        Contract (observed; no caller in src/). Order: none.
        """

    @abstractmethod
    async def list_users_by_provider(self, provider: str) -> List[str]:
        """Return the user_id of every record of `provider`, each user once, needs_reconnect ones included.

        Order: none; callers iterate. Limit: none.
        Legacy: a record without user_id is skipped (observed).
        """
