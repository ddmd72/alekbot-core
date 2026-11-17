"""Where a session's overflow goes (COMPANION_AGENTS_RFC §9 item 3): the channel binding decides whether a batch that
leaves the sliding window goes to Alek's consolidation pipeline or to a companion's extraction pipeline, and how large
the window of a companion channel is. Built before the `ServiceContainer`, whose session store takes the overflow
callback and the threshold resolver at construction."""
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..adapters.firestore_channel_binding_adapter import FirestoreChannelBindingAdapter
from ..adapters.firestore_companion_extraction_queue import FirestoreCompanionExtractionQueue
from ..adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from ..config.environment import EnvironmentConfig
from ..ports.task_queue import TaskQueue
from ..ports.user_repository import UserRepository
from ..services.channel_binding_service import ChannelBindingService
from ..services.companion_window_resolver import CompanionWindowResolver
from ..services.overflow_routing_service import OverflowRoutingService
from ..utils.logger import logger


@dataclass(frozen=True)
class SessionRouting:
    """The two queues an overflow lands in, the channel bindings, and the session store's two callbacks."""

    consolidation_queue: FirestoreConsolidationQueue
    companion_extraction_queue: FirestoreCompanionExtractionQueue
    channel_binding_adapter: FirestoreChannelBindingAdapter
    channel_binding_service: ChannelBindingService
    overflow_callback: Callable[..., Any]
    threshold_resolver: Callable[..., Any]


def build_session_routing(db_client: Any, env_config: EnvironmentConfig, *, user_repo: UserRepository,
                          task_queue: Optional[TaskQueue]) -> SessionRouting:
    logger.info("🌊 Initializing session routing: consolidation queue, channel bindings, overflow routing...")
    consolidation_queue = FirestoreConsolidationQueue(db_client=db_client, env_config=env_config)
    channel_binding_adapter = FirestoreChannelBindingAdapter(db_client=db_client, env_config=env_config)
    channel_binding_service = ChannelBindingService(port=channel_binding_adapter)
    companion_extraction_queue = FirestoreCompanionExtractionQueue(db_client=db_client, env_config=env_config)
    overflow_routing_service = OverflowRoutingService(
        channel_binding_service=channel_binding_service,
        user_repo=user_repo,
        consolidation_queue=consolidation_queue,
        companion_extraction_queue=companion_extraction_queue,
        task_queue=task_queue,
    )
    return SessionRouting(
        consolidation_queue=consolidation_queue,
        companion_extraction_queue=companion_extraction_queue,
        channel_binding_adapter=channel_binding_adapter,
        channel_binding_service=channel_binding_service,
        overflow_callback=overflow_routing_service.route_overflow,
        threshold_resolver=CompanionWindowResolver(channel_binding_service).resolve,
    )
