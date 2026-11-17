"""How the main service hands files to users: the private media bucket, `/f/<token>` capability links and `/s/<code>`
short links over them, and the document delivery both the chat handler and the agent worker use. Everything but the
token service exists only with GCS_MEDIA_BUCKET."""
from dataclasses import dataclass
from typing import Any, Optional

from ..adapters.firestore_short_link_repository import FirestoreShortLinkRepository
from ..adapters.gcs_media_adapter import GcsMediaAdapter
from ..config.environment import EnvironmentConfig
from ..services.document_delivery_service import DocumentDeliveryService
from ..services.file_access_token_service import FileAccessTokenService
from ..services.file_link_service import FileLinkService
from ..services.short_link_service import ShortLinkService
from ..utils.logger import logger


@dataclass(frozen=True)
class FileDelivery:
    media_storage: Optional[GcsMediaAdapter]
    file_token_service: FileAccessTokenService
    link_service: Optional[FileLinkService]
    short_link_service: Optional[ShortLinkService]
    doc_delivery_service: Optional[DocumentDeliveryService]


def build_file_delivery(config: dict, db_client: Any, env_config: EnvironmentConfig, *, token_secret: str,
                        service_url: str) -> FileDelivery:
    """`token_secret` is the Cabinet session's HS256 secret (the capability tokens reuse it); `service_url` the base of
    every link. The media adapter signs URLs keyless, via IAM signBlob as the runtime's own identity."""
    bucket = config.get("GCS_MEDIA_BUCKET", "")
    media = GcsMediaAdapter(bucket_name=bucket) if bucket else None
    tokens = FileAccessTokenService(secret_key=token_secret)
    if media is None:
        logger.info("ℹ️ DocumentDeliveryService disabled (GCS_MEDIA_BUCKET not set)")
        return FileDelivery(media_storage=None, file_token_service=tokens, link_service=None, short_link_service=None,
                            doc_delivery_service=None)
    links = FileLinkService(token_service=tokens, base_url=service_url)
    short_links = ShortLinkService(
        repository=FirestoreShortLinkRepository(db_client=db_client, env_config=env_config), base_url=service_url)
    delivery = DocumentDeliveryService(storage=media, link_service=links)
    logger.info("✅ DocumentDeliveryService initialized")
    return FileDelivery(media_storage=media, file_token_service=tokens, link_service=links,
                        short_link_service=short_links, doc_delivery_service=delivery)
