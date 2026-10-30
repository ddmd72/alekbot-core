"""CredentialRevocationService — removing a stored grant and the provider-side revoke rule (MULTI_DRIVE_RFC §4.3).

Our record is always deleted. The provider grant is revoked only when the realm's revoke ends the whole grant
(`GRANT_WIDE`) and no credential of the same provider account remains, across users, so removing one Google service
never kills the grant another service still uses.
"""
from typing import Mapping

from src.domain.revocation_scope import RevocationScope
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.ports.oauth_provider_port import OAuthProviderPort
from src.utils.logger import logger


class CredentialRevocationService:
    def __init__(self, credentials: OAuthCredentialsPort, edges: Mapping[str, OAuthProviderPort]):
        self._credentials = credentials
        self._edges = edges

    async def remove(self, user_id: str, credential_id: str) -> bool:
        """Delete the credential; revoke at the provider when it was the last one of a grant-wide account.
        False when there is no such credential. A revoke failure is logged and the removal still counts."""
        credential = await self._credentials.get(user_id, credential_id)
        if credential is None:
            return False
        await self._credentials.delete(user_id, credential_id)
        edge = self._edges.get(credential.account.realm)
        if edge is None or edge.revocation_scope is not RevocationScope.GRANT_WIDE:
            return True
        remaining = await self._credentials.list_by_account(credential.account.realm, credential.account.subject)
        if remaining:
            return True
        try:
            await edge.revoke(credential)
        except Exception as exc:
            logger.warning(f"Provider revoke failed: user={user_id[:8]} kind={credential.kind} "
                           f"credential={credential_id[:8]}: {type(exc).__name__}")
        return True
