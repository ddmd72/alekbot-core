"""A real `ExternalTaskAgent` (the task specialist, EXTERNAL_TASK_QUEUE_RFC §5.3) over a scripted model and the queue
of `tests/support/external_task_world.py`.

`specialist_rig(llm, world, ...)` builds it as the factory does for Alex Doe (`usr-1`): the real service unless
`service=None` (a deployment without a task queue), `StubPromptBuilder` unless one is given, and the `notifications`
that post a reminder create's receipt (none unless given). `specialist_message(query,
...)` is the message the coordinator delivers when Smart delegates `work_with_external_tasks` on the owner's chat turn:
the call chain is the specialist alone, the turn is interactive, and the origin is the garden channel; every keyword
replaces or adds a context key (`None` removes one).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.agents.external_task_agent import ExternalTaskAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import EXTERNAL_TASKS
from src.ports.llm_port import LLMPort
from src.ports.prompt_builder_port import PromptBuilderPort
from tests.support.external_task_world import ACCOUNT_ID, USER_ID, ExternalTaskWorld
from tests.support.scripted_llm import execution_context
from tests.support.tool_e2e import StubPromptBuilder

_KEEP = object()


def specialist_rig(llm: LLMPort, world: Optional[ExternalTaskWorld], *, service: Any = _KEEP,
                   prompt_builder: Optional[PromptBuilderPort] = None, thinking: Optional[str] = None,
                   coordinator: Optional[AgentCoordinator] = None, notifications: Any = None) -> ExternalTaskAgent:
    agent = ExternalTaskAgent(
        config=AgentConfig(agent_id=f"{EXTERNAL_TASKS.agent_id}_{USER_ID}", agent_type=EXTERNAL_TASKS.agent_type,
                           timeout_ms=90_000),
        execution_context=execution_context(llm, EXTERNAL_TASKS.agent_type),
        service=(world.service if world is not None else None) if service is _KEEP else service,
        prompt_builder=prompt_builder if prompt_builder is not None else StubPromptBuilder("TASK SPECIALIST PROMPT"),
        thinking=thinking,
        notifications=notifications,
    )
    agent.coordinator = coordinator if coordinator is not None else AgentCoordinator()
    return agent


def specialist_message(query: str, **context: Any) -> AgentMessage:
    ctx: Dict[str, Any] = {
        "user_id": USER_ID, "account_id": ACCOUNT_ID, "session_id": f"{USER_ID}:C-garden",
        "origin_platform": "slack", "origin_channel_id": "C-garden", "interactive_delivery": True,
        AgentCoordinator.CALL_CHAIN_KEY: [EXTERNAL_TASKS.agent_id],
    }
    for key, value in context.items():
        if value is None:
            ctx.pop(key, None)
        else:
            ctx[key] = value
    return AgentMessage.create(sender="coordinator", recipient=f"{EXTERNAL_TASKS.agent_id}_{USER_ID}",
                               intent=AgentIntent.QUERY, payload={"query": query, "intent": "work_with_external_tasks"},
                               context=ctx)
