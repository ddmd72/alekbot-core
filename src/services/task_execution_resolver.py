from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Dict
from dataclasses import dataclass
from ..domain.task_complexity import TaskComplexity
from ..domain.complexity_settings import ComplexitySettings, DEFAULT_COMPLEXITY_SETTINGS
from ..domain.user import UserBotConfig
from ..ports.llm_port import AgentExecutionContext
from ..utils.logger import logger

if TYPE_CHECKING:
    from .agent_context_builder import AgentContextBuilder

@dataclass
class TaskExecutionOverride:
    execution_context: AgentExecutionContext
    thinking_effort: Optional[str]
    intent_remap: Dict[str, str]

class TaskExecutionResolver:
    def __init__(self, context_builder: "AgentContextBuilder"):
        self.context_builder = context_builder

    def resolve(
        self,
        context: dict,
        config: UserBotConfig,
        agent_type: str = "smart"
    ) -> Optional[TaskExecutionOverride]:
        complexity_str = context.get("task_complexity")
        if not complexity_str:
            return None
            
        try:
            complexity = TaskComplexity(complexity_str)
        except ValueError:
            logger.warning(
                "invalid_task_complexity",
                extra={
                    "event": "invalid_task_complexity",
                    "task_complexity": complexity_str,
                }
            )
            return None

        default_settings = DEFAULT_COMPLEXITY_SETTINGS.get(complexity)
        if not default_settings:
            return None

        user_override = config.complexity_settings_overrides.get(complexity)
        
        merged_tier = user_override.tier if user_override and user_override.tier else default_settings.tier
        merged_thinking = user_override.thinking_effort if user_override and user_override.thinking_effort is not None else default_settings.thinking_effort
        merged_remap = user_override.intent_remap if user_override and user_override.intent_remap else default_settings.intent_remap
        merged_provider = user_override.provider_override if user_override and user_override.provider_override else default_settings.provider_override

        settings = ComplexitySettings(
            tier=merged_tier,
            thinking_effort=merged_thinking,
            intent_remap=merged_remap,
            provider_override=merged_provider
        )

        execution_context = self.context_builder.resolve_for_task(
            agent_type=agent_type,
            config=config,
            settings=settings
        )

        return TaskExecutionOverride(
            execution_context=execution_context,
            thinking_effort=merged_thinking,
            intent_remap=merged_remap
        )
