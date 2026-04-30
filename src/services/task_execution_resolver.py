"""
TaskExecutionResolver — maps a per-call ``task_complexity`` value to an
``ExecutionOverride``.

Reads ``message.context["task_complexity"]`` (a string from the
``TaskComplexity`` enum), merges per-user overrides on top of the system
defaults, and resolves the resulting ``ComplexitySettings`` into an
``AgentExecutionContext`` via ``AgentContextBuilder``. Returns ``None`` when
no complexity is requested or when the value is invalid (the agent will
then fall back to its default execution context).

The returned ``ExecutionOverride`` is a frozen value object — agents
consume it locally during one ``execute()`` call and never persist it on
``self.*`` (see ``docs/04_solution_strategy/decisions/per_call_execution_context.md``).

The value object lives in this module (not a separate file) because:

  - ``domain/`` cannot import from ``ports/`` (ExecutionOverride references
    ``AgentExecutionContext`` which lives in ``ports/llm_port.py``).
  - ``ports/`` cannot import from other ``ports/`` (REQ-ARCH-06).
  - ``services/`` cannot import from other ``services/`` (REQ-ARCH-22).

Co-locating the value object with its sole producer (the resolver)
respects every layer rule and adds no cross-module coupling. Consumers
(SmartResponseAgent, etc.) import ``ExecutionOverride`` from this module.

See: docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 4
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Optional

from ..domain.task_complexity import TaskComplexity
from ..domain.complexity_settings import ComplexitySettings, DEFAULT_COMPLEXITY_SETTINGS
from ..domain.user import UserBotConfig
from ..ports.llm_port import AgentExecutionContext
from ..utils.logger import logger

if TYPE_CHECKING:
    from .agent_context_builder import AgentContextBuilder


@dataclass(frozen=True)
class ExecutionOverride:
    """Immutable per-call override of an agent's default execution parameters.

    Carried through ``AgentMessage.context["execution_override"]`` (or
    returned by ``TaskExecutionResolver.resolve``) and consumed inside
    ``execute()``. Never mutates the agent instance.

    Attributes:
        execution_context: provider/model/tier resolved for this call.
        thinking_effort: "low" / "medium" / "high"; ``None`` means "no
            override on top of agent default".
        intent_remap: dispatch-time substitutions applied by
            ``DelegationEngine`` for this call only (e.g.
            ``{"search_web": "search_web_light"}``). Empty dict = no remap.
    """
    execution_context: AgentExecutionContext
    thinking_effort: Optional[str] = None
    intent_remap: Dict[str, str] = field(default_factory=dict)


class TaskExecutionResolver:
    def __init__(self, context_builder: "AgentContextBuilder"):
        self.context_builder = context_builder

    def resolve(
        self,
        context: dict,
        config: UserBotConfig,
        agent_type: str = "smart"
    ) -> Optional[ExecutionOverride]:
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

        return ExecutionOverride(
            execution_context=execution_context,
            thinking_effort=merged_thinking,
            intent_remap=merged_remap or {},
        )
