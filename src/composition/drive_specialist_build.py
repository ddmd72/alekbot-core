"""
Building the drive specialist for one user (docs/10_rfcs/DRIVE_SPECIALIST_RFC.md §5.1, §5.5): the one place that
turns a user's config and their DriveOperations into a DriveAgent. UserAgentFactory's lazy builder calls it, and so do
the drive evals (`scripts/validation/drive_eval/`), which build the specialist over the fixture drive exactly as
production builds it over the user's.

Public interface:
    build_drive_specialist(user_id, *, context_builder, prompt_builder, user_config, operations, drive_search)
        -> DriveAgent

Provider, tier and fallback come from AgentContextBuilder ("drive": claude BALANCED, failover openai PERFORMANCE);
thinking is the user's `agent_thinking["drive"]` when set, else DriveAgentConfig's default ("medium").
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ..agents.drive_agent import DriveAgent
from ..domain.agent import AgentConfig
from ..infrastructure.agent_config import DRIVE_SPECIALIST as DRIVE_CFG
from ..infrastructure.agent_manifest import DRIVE_SPECIALIST

if TYPE_CHECKING:
    from ..domain.user import UserBotConfig
    from ..infrastructure.drive_operations import DriveOperations
    from ..services.drive_search_service import DriveSearchService


def build_drive_specialist(user_id: str, *, context_builder: Any, prompt_builder: Any, user_config: "UserBotConfig",
                           operations: "DriveOperations",
                           drive_search: Optional["DriveSearchService"]) -> DriveAgent:
    return DriveAgent(
        config=AgentConfig(
            agent_id=f"{DRIVE_SPECIALIST.agent_id}_{user_id}",
            agent_type=DRIVE_SPECIALIST.agent_type,
            timeout_ms=DRIVE_CFG.timeout_ms,
            capabilities=["drive"],
        ),
        execution_context=context_builder.build(DRIVE_SPECIALIST.agent_type, user_config),
        prompt_builder=prompt_builder,
        operations=operations,
        drive_search=drive_search,
        user_timezone=user_config.timezone,
        indexing_enabled=user_config.drive_indexing_enabled,
        thinking_effort=user_config.get_thinking_for_agent(DRIVE_SPECIALIST.agent_type) or DRIVE_CFG.thinking_effort,
    )
