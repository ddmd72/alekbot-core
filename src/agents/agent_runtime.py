"""
The per-process collaborators the composition root binds to an agent it builds, through `BaseAgent.bind_runtime`.

They cannot be constructor arguments without touching every agent's factory, so they arrive in one named call before
the agent is registered, instead of writes to its private attributes (composition-root assessment D4). A collaborator
left `None` turns its feature off, as for an agent never bound.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from ..ports.prompt_content_store import PromptContentStore
from ..ports.quota_service import QuotaService

if TYPE_CHECKING:
    from ..infrastructure.agent_coordinator import AgentCoordinator


@dataclass(frozen=True)
class AgentRuntime:
    """coordinator: delegation to other agents; prompt_content_store: LLM content capture; quota_service: billing."""

    coordinator: Optional[AgentCoordinator] = None
    prompt_content_store: Optional[PromptContentStore] = None
    quota_service: Optional[QuotaService] = None
