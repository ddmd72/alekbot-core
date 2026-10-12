"""
Prompt Design System v3 wiring shared by the main service (`ServiceContainer`) and the drive indexer
(`indexer_main.py`, docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.2).

The indexer needs the Firestore prompt for its two agents but must not build `ServiceContainer` (every agent, LLM
adapter and chat surface, `indexer_main.py` docstring). One builder keeps the two services assembling prompts from
the same repositories, the same security port and the same collections. It raises on failure: `ServiceContainer`
keeps its own degrade-to-None handling around it, the indexer fails fast (no fallback prompts, CLAUDE.md).
"""
from __future__ import annotations

from typing import Any, Tuple

from ..adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
from ..adapters.prompt_v3.firestore_blueprint_repository import FirestoreBlueprintRepository
from ..adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
from ..adapters.security.composite_adapter import CompositeAdapter
from ..adapters.security.regex_adapter import RegexSecurityAdapter
from ..config.environment import EnvironmentConfig
from ..services.prompt_v3.biographical_formatter import BiographicalFactsFormatter
from ..services.prompt_v3.context_formatter import ContextFormatter
from ..services.prompt_v3.prompt_assembly_service import PromptAssemblyService


def build_prompt_assembly_service(db_client: Any,
                                  env_config: EnvironmentConfig) -> Tuple[PromptAssemblyService,
                                                                          FirestoreAgentProfileRepository]:
    """(assembly_service, profile_repo) over the environment's prompt collections."""
    security_port = CompositeAdapter(adapters=[RegexSecurityAdapter()], strategy="worst_case")
    token_repo = FirestoreTokenRepository(
        db=db_client,
        system_collection=f"{env_config.domain_prompt_tokens_collection}_system",
        user_collection=f"{env_config.domain_prompt_tokens_collection}_user",
        security_port=security_port,
    )
    blueprint_repo = FirestoreBlueprintRepository(
        db=db_client,
        collection_name=env_config.domain_prompt_blueprints_collection,
    )
    profile_repo = FirestoreAgentProfileRepository(
        db=db_client,
        profiles_collection=env_config.domain_prompt_profiles_collection,
        overrides_collection=env_config.domain_prompt_overrides_collection,
    )
    service = PromptAssemblyService(
        token_repo=token_repo,
        blueprint_repo=blueprint_repo,
        profile_repo=profile_repo,
        security_port=security_port,
        formatter=ContextFormatter(),
        bio_formatter=BiographicalFactsFormatter(),
    )
    return service, profile_repo
