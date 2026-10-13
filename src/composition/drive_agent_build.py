"""
Building one drive model agent for one user on the indexer (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.2, §25.4.4):
the Search v2 runners' shared step, the same as DriveReaderRunner's and DriveTriageRunner's own.

Public interface:
    async build_drive_agent(agent_cls, agent_type, user_id, *, context_builder, user_repo, prompt_builder,
                            quota_service) -> agent          raises DriveModelUnavailable (no user, no provider)

The user's UserBotConfig picks provider, tier and thinking through AgentContextBuilder (D7); the quota service is set
after construction as UserAgentFactory does, without which `_flush_billing` is a silent no-op.
"""
from __future__ import annotations

from typing import Any, Optional, Type, TypeVar

from ..domain.agent import AgentConfig
from ..domain.drive_extraction import DriveModelUnavailable
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.quota_service import QuotaService
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

A = TypeVar("A")


async def build_drive_agent(agent_cls: Type[A], agent_type: str, user_id: str, *, context_builder: Any,
                            user_repo: UserRepository, prompt_builder: PromptBuilderPort,
                            quota_service: Optional[QuotaService]) -> A:
    user = await user_repo.get_user(user_id)
    if user is None:
        logger.error(f"[{agent_type}] user {user_id[:8]} not found")
        raise DriveModelUnavailable("user not found", charges_file=False)
    try:
        context = context_builder.build(agent_type, user.config)
    except Exception as exc:
        logger.error(f"[{agent_type}] cannot build the agent for user {user_id[:8]}: {type(exc).__name__}: {exc}")
        raise DriveModelUnavailable(f"{agent_type} provider unavailable", charges_file=False) from exc
    agent = agent_cls(  # type: ignore[call-arg]
        AgentConfig(agent_id=f"{agent_type}_{user_id}", agent_type=agent_type, capabilities=[agent_type]),
        context, prompt_builder, thinking=user.config.get_thinking_for_agent(agent_type))
    setattr(agent, "_quota_service", quota_service)
    return agent
