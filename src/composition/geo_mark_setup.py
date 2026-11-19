"""Wiring of geo marks (GEO_MARKS_RFC §4.3, §4.4): the link store, the optional Google reverse geocoder and the service.

The geocoder is built only when `GOOGLE_SEARCH_API_KEY` is set (the key the Maps MCP client already uses); without it
every mark has no address. No task queue (a local run without a GCP project) means no service: `$geo` and the
Telegram pin then answer that location sharing is unavailable.
"""
from typing import Any, Optional

from ..adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from ..adapters.google_geocoding_adapter import GoogleGeocodingAdapter
from ..config.environment import EnvironmentConfig
from ..ports.localization_port import LocalizationPort
from ..ports.notification_channel_factory_port import NotificationChannelFactoryPort
from ..ports.reverse_geocoding_port import ReverseGeocodingPort
from ..ports.session_store import SessionStore
from ..ports.task_queue import TaskQueue
from ..ports.user_repository import UserRepository
from ..services.geo_mark_service import GeoMarkService
from ..utils.logger import logger


def build_geo_mark_service(config: dict, db_client: Any, env_config: EnvironmentConfig, *, digest_key: bytes,
                           service_url: str, session_store: SessionStore,
                           channel_factory: NotificationChannelFactoryPort, task_queue: Optional[TaskQueue],
                           user_repo: UserRepository, localization_port: LocalizationPort) -> Optional[GeoMarkService]:
    if task_queue is None:
        logger.info("ℹ️ Geo marks off: no task queue")
        return None
    geocoder: Optional[ReverseGeocodingPort] = None
    api_key = config.get("GOOGLE_SEARCH_API_KEY")
    if api_key:
        geocoder = GoogleGeocodingAdapter(api_key)
    else:
        logger.info("ℹ️ Geo marks without addresses: GOOGLE_SEARCH_API_KEY not set")
    return GeoMarkService(
        links=FirestoreEphemeralStore(db_client, env_config.geo_mark_links_collection),
        geocoder=geocoder, sessions=session_store, channels=channel_factory, task_queue=task_queue,
        users=user_repo, localization=localization_port, digest_key=digest_key, base_url=service_url,
    )
