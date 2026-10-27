"""
FirestoreShortLinkRepository — persists short-code → URL mappings.

Collection: {env_prefix}short_links  (doc ID = code)

`create_if_absent` uses Firestore's atomic `create()` (fails if the doc
already exists) rather than check-then-set, so a code collision can't race.

Firestore's native TTL policy on `expires_at` reclaims old docs, but that
sweep is best-effort and can lag — `resolve()` re-checks `expires_at` itself
so correctness never depends on the sweep's timing.
"""
from datetime import datetime, timezone
from typing import Optional

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.async_client import AsyncClient

from ..config.environment import EnvironmentConfig
from ..domain.short_link import ShortLink
from ..ports.short_link_repository_port import ShortLinkRepositoryPort
from ..utils.logger import logger


class FirestoreShortLinkRepository(ShortLinkRepositoryPort):
    """Firestore mechanics behind the port's contract.

    Collection `{firestore_collection_prefix}short_links` (no `EnvironmentConfig` property), document id = the code,
    fields target_url and expires_at. `create_if_absent` is one `create` (AlreadyExists = the code is taken). A
    Firestore TTL policy on `expires_at` is meant to reclaim old documents (the deployment README's TTL table does not
    list this collection: whether the policy exists is unverified); the sweep lags, so `resolve` re-checks the expiry
    itself. No query is issued, so no index is needed.
    """

    def __init__(self, db_client: AsyncClient, env_config: EnvironmentConfig) -> None:
        self._collection = db_client.collection(
            f"{env_config.firestore_collection_prefix}short_links"
        )

    async def create_if_absent(self, link: ShortLink) -> bool:
        try:
            await self._collection.document(link.code).create({
                "target_url": link.target_url,
                "expires_at": link.expires_at,
            })
            return True
        except AlreadyExists:
            logger.info("FirestoreShortLinkRepository: code collision on '%s'", link.code)
            return False

    async def resolve(self, code: str) -> Optional[ShortLink]:
        doc = await self._collection.document(code).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        expires_at = data["expires_at"]
        if expires_at <= datetime.now(timezone.utc):
            return None
        return ShortLink(code=code, target_url=data["target_url"], expires_at=expires_at)
