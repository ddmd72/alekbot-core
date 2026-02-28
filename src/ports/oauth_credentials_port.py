"""
OAuthCredentialsPort — persist OAuth tokens, provider-agnostic.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.email import OAuthCredentials


class OAuthCredentialsPort(ABC):

    @abstractmethod
    async def get_credentials(
        self, user_id: str, provider: str
    ) -> Optional[OAuthCredentials]:
        """
        Returns stored credentials for user+provider, or None if not connected.
        Does NOT check token expiry — caller is responsible for refresh if needed.
        """

    @abstractmethod
    async def save_credentials(self, credentials: OAuthCredentials) -> None:
        """
        Upsert credentials. Doc ID: {user_id}_{provider}.
        Called after initial OAuth flow and after each token refresh.
        """

    @abstractmethod
    async def revoke_credentials(self, user_id: str, provider: str) -> None:
        """
        Delete stored tokens for user+provider.
        Caller is responsible for revoking the token at the provider first
        (EmailProviderPort.revoke_token or provider API directly).
        """

    @abstractmethod
    async def is_connected(self, user_id: str, provider: str) -> bool:
        """
        Quick existence check — does user have stored credentials for this provider?
        Does not validate token expiry.
        """

    @abstractmethod
    async def list_connected_providers(self, user_id: str) -> List[str]:
        """
        All providers with stored credentials for this user (e.g. ["gmail"]).
        Used by EmailIndexingService to fan-out across all connected providers.
        """
