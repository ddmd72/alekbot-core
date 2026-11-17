"""The per-user agents: the `UserAgentFactory` over the container's ports and the subsystems built before it, the
long-running turns both chat platforms and the factory share (LONG_RUNNING_TURNS_RFC), and the language preference
writer, which refreshes a user's agents after a change. The coordinator gets the factory afterwards
(`set_agent_factory`, the one real router-factory cycle)."""
from dataclasses import dataclass
from typing import Any, Optional

from anthropic import AsyncAnthropic

from ..adapters.firestore_long_turn_registry import FirestoreLongTurnRegistry
from ..config.environment import EnvironmentConfig
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..services.language_preference_service import LanguagePreferenceService
from ..services.long_turn_service import LongTurnService
from ..services.provider_registry import ProviderRegistry
from ..utils.logger import logger
from .boot_setup import BootChecks
from .companion_setup import Companions
from .identity_setup import Identity
from .language_setup import Languages
from .memory_search_setup import MemorySearch
from .operations_setup import Operations
from .user_agent_factory import UserAgentFactory


@dataclass(frozen=True)
class Agents:
    factory: UserAgentFactory
    long_turn_service: LongTurnService
    language_preferences: LanguagePreferenceService


def build_agents(config: dict, db_client: Any, env_config: EnvironmentConfig, *, container: Any,
                 coordinator: AgentCoordinator, boot: BootChecks, identity: Identity, ops: Operations,
                 job_registry: ProviderRegistry, image_registry: ProviderRegistry, video_registry: ProviderRegistry,
                 companions: Companions, notification_service: Any, languages: Languages,
                 memory_search: MemorySearch) -> Agents:
    """One long-turn registry for the factory (Smart's running-jobs note, `cancel_long_turn`) and the service the chat
    adapters read; one Anthropic client for every deep-research runner agent (the agent does not build the SDK)."""
    long_turn_registry = FirestoreLongTurnRegistry(db_client, env_config.long_turns_collection)
    long_turn_service = LongTurnService(registry=long_turn_registry, session_store=container.session_store)
    anthropic_client: Optional[AsyncAnthropic] = None
    if config.get("ANTHROPIC_API_KEY"):
        anthropic_client = AsyncAnthropic(api_key=config["ANTHROPIC_API_KEY"])

    logger.info("🏭 Initializing User Agent Factory...")
    agent_factory = UserAgentFactory(
        config=config,
        fact_search=memory_search.fact_search,
        deploy_topology=boot.deploy_topology,
        coordinator=coordinator,
        user_repo=identity.user_repo,
        account_repo=identity.account_repo,
        **container.agent_services(),
        job_registry=job_registry,
        image_registry=image_registry,
        video_registry=video_registry,
        task_queue=ops.task_queue,
        anthropic_client=anthropic_client,
        quota_service=ops.quota_service,
        companion_context_assembler=companions.context_assembler,
        notification_service=notification_service,
        long_turn_registry=long_turn_registry,
        localization=languages.localization,
        language_service=languages.resolver,
        alert_sink=ops.alert_sink,
        video_duration=boot.video_duration,
    )
    language_preferences = LanguagePreferenceService(
        user_repo=identity.user_repo,
        profile_repo=container.profile_repo,
        prompt_builder=container.assembly_service,
        resolver=languages.resolver,
        notification_service=notification_service,
        ensure_agents=agent_factory.ensure_agents_for_user,
    )
    return Agents(factory=agent_factory, long_turn_service=long_turn_service, language_preferences=language_preferences)
