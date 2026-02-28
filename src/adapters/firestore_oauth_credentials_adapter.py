"""
FirestoreOAuthCredentialsAdapter — persists OAuth tokens per user/provider.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.

Doc ID: {user_id}_{provider}
Collection: {env}_oauth_credentials
"""

from typing import List, Optional

from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.email import OAuthCredentials
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..utils.logger import logger


class FirestoreOAuthCredentialsAdapter(OAuthCredentialsPort):

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self.db = db_client
        collection_name = env_config.oauth_credentials_collection
        self.collection = self.db.collection(collection_name)
        logger.info(f"📂 OAuthCredentials adapter initialized. Collection: {collection_name}")

    @staticmethod
    def _doc_id(user_id: str, provider: str) -> str:
        return f"{user_id}_{provider}"

    @staticmethod
    def _to_firestore(creds: OAuthCredentials) -> dict:
        return {
            "user_id": creds.user_id,
            "provider": creds.provider,
            "access_token": creds.access_token,
            "refresh_token": creds.refresh_token,
            "token_expiry": creds.token_expiry,
            "scopes": creds.scopes,
            "email_address": creds.email_address,
        }

    @staticmethod
    def _from_firestore(data: dict) -> OAuthCredentials:
        return OAuthCredentials(
            user_id=data["user_id"],
            provider=data["provider"],
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            token_expiry=data["token_expiry"],
            scopes=data["scopes"],
            email_address=data["email_address"],
        )

    async def get_credentials(
        self, user_id: str, provider: str
    ) -> Optional[OAuthCredentials]:
        doc_id = self._doc_id(user_id, provider)
        doc = await self.collection.document(doc_id).get()
        if not doc.exists:
            return None
        return self._from_firestore(doc.to_dict())

    async def save_credentials(self, credentials: OAuthCredentials) -> None:
        doc_id = self._doc_id(credentials.user_id, credentials.provider)
        await self.collection.document(doc_id).set(self._to_firestore(credentials))
        logger.debug(
            f"💾 OAuth credentials saved: user={credentials.user_id[:8]} "
            f"provider={credentials.provider}"
        )

    async def revoke_credentials(self, user_id: str, provider: str) -> None:
        doc_id = self._doc_id(user_id, provider)
        await self.collection.document(doc_id).delete()
        logger.info(
            f"🗑️ OAuth credentials revoked: user={user_id[:8]} provider={provider}"
        )

    async def is_connected(self, user_id: str, provider: str) -> bool:
        doc_id = self._doc_id(user_id, provider)
        doc = await self.collection.document(doc_id).get()
        return doc.exists

    async def list_connected_providers(self, user_id: str) -> List[str]:
        query = self.collection.where(filter=FieldFilter("user_id", "==", user_id))
        providers = []
        async for doc in query.stream():
            data = doc.to_dict()
            providers.append(data["provider"])
        return providers
