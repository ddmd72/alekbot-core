"""
OAuthProviderPort — one OAuth edge per realm (MULTI_DRIVE_RFC §5.1): the consent URL, the code exchange and the
provider-side revoke. Token refresh is not here: it stays with the data adapters' token providers (E3).
"""

from abc import ABC, abstractmethod

from src.domain.connector_spec import ConnectorSpec
from src.domain.oauth_credential import OAuthCredential
from src.domain.oauth_flow import OAuthFlow
from src.domain.oauth_grant import GrantResult
from src.domain.revocation_scope import RevocationScope


class OAuthProviderPort(ABC):
    """The OAuth edge of one realm. Services hold no OAuth parameter name: the edge maps `flow.intent` and
    `flow.hint_label` to its realm's parameters (E16)."""

    @property
    @abstractmethod
    def realm(self) -> str:
        """The realm served ("google", "microsoft"): data, compared, never branched on."""

    @property
    @abstractmethod
    def revocation_scope(self) -> RevocationScope:
        """What a provider-side revoke of this realm ends (§4.3)."""

    @abstractmethod
    def authorization_url(self, flow: OAuthFlow, spec: ConnectorSpec, redirect_uri: str, *, state: str) -> str:
        """The consent URL for `flow`; `state` is the raw OAuth state (the stored flow keeps only its keyed digest)."""

    @abstractmethod
    async def exchange(self, code: str, flow: OAuthFlow, spec: ConnectorSpec, redirect_uri: str) -> GrantResult:
        """Exchange the authorization code: the tokens, the granted scopes and the consenting account. Raises when
        the account identity cannot be read (nothing is stored then)."""

    @abstractmethod
    async def revoke(self, credential: OAuthCredential) -> None:
        """Revoke `credential`'s grant at the provider."""
