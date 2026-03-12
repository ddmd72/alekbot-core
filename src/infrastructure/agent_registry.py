"""
Agent Registry
==============

Central registry for dynamic agent discovery.
Maps intents to agent descriptors with per-intent execution modes.

Enables adding new specialist agents without modifying orchestrator agents (Quick/Smart).

AgentDescriptor has two parts:
  A) capabilities   — what this agent offers to others (intents dict, descriptions, internal flag)
  B) requirements   — what this agent needs to function (allowed_intents filter, intent_remap)

Orchestrators (Quick, Smart) construct their own descriptor at init time to declare B.
Specialists register their descriptor in main.py to publish A.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..utils.logger import logger


class ExecutionMode(str, Enum):
    """Execution mode for an agent intent."""
    SYNC = "sync"    # Execute immediately, return result
    ASYNC = "async"  # Enqueue to Cloud Tasks, return ack


@dataclass
class AgentDescriptor:
    """
    Unified agent capability and requirement declaration.

    Part A — what this agent offers to others:
        capabilities:             intent → execution mode mapping
        capability_descriptions:  per-intent human-readable descriptions
        internal:                 if True, intents are NOT shown to LLMs;
                                  used for implementation-level agents
                                  (e.g. web_search_light — Quick calls it
                                  internally via intent_remap, but LLMs see
                                  only "search_web")
        description:              agent-level fallback description

    Part B — what this agent needs (for orchestrators that delegate):
        allowed_intents:  frozenset of intent names this agent may call,
                          or None to allow all non-internal intents.
        intent_remap:     dispatch-time substitution applied AFTER intent
                          selection. e.g. {"search_web": "search_web_light"}
                          means: LLM picks search_web, coordinator receives
                          search_web_light. Purely internal routing.

    Example — specialist (only Part A):
        AgentDescriptor(
            agent_id="memory_search_agent",
            agent_type="memory_search",
            capabilities={"search_memory": ExecutionMode.SYNC},
            capability_descriptions={"search_memory": "Semantic knowledge base search"},
        )

    Example — orchestrator (only Part B):
        AgentDescriptor(
            agent_id="quick_response_agent",
            agent_type="quick_response",
            capabilities={},
            allowed_intents=None,        # all non-internal
            intent_remap={"search_web": "search_web_light"},
        )
    """
    # Identity
    agent_id: str
    agent_type: str = ""

    # Part A: What I offer
    capabilities: Dict[str, ExecutionMode] = field(default_factory=dict)
    capability_descriptions: Dict[str, str] = field(default_factory=dict)
    internal: bool = False
    description: str = ""
    requires_auth: bool = False

    # Part B: What I need (orchestrators only)
    allowed_intents: Optional[frozenset] = None
    intent_remap: Dict[str, str] = field(default_factory=dict)

    # Cloud Tasks dispatch deadline for ASYNC intents (seconds). None = Cloud Tasks default (600s).
    dispatch_deadline_s: Optional[int] = None


# Backward-compatible alias — existing callers using AgentManifest continue to work.
AgentManifest = AgentDescriptor


class AgentRegistry:
    """
    Central registry for agent discovery.

    Stores AgentDescriptor entries and maps intent names to descriptors.
    Used by AgentCoordinator.handle_delegation() to route
    delegate_to_specialist calls without hardcoded maps in orchestrators.
    """

    def __init__(self) -> None:
        self._agents: Dict[str, AgentDescriptor] = {}
        self._intent_to_agent: Dict[str, str] = {}  # intent → agent_id

    def register(self, descriptor: AgentDescriptor) -> None:
        """
        Register an agent descriptor.

        Overwrites existing registration for the same agent_id.
        Logs a warning if an intent is already claimed by another agent.
        """
        self._agents[descriptor.agent_id] = descriptor

        for intent in descriptor.capabilities:
            existing = self._intent_to_agent.get(intent)
            if existing and existing != descriptor.agent_id:
                logger.warning(
                    f"Intent '{intent}' was registered by '{existing}', "
                    f"overwriting with '{descriptor.agent_id}'"
                )
            self._intent_to_agent[intent] = descriptor.agent_id

        logger.info(
            f"Registered agent descriptor: {descriptor.agent_id} "
            f"(intents={list(descriptor.capabilities.keys())}, internal={descriptor.internal})"
        )

    def get_agent_for_intent(self, intent: str) -> Optional[AgentDescriptor]:
        """Return the descriptor for the agent that handles this intent, or None."""
        agent_id = self._intent_to_agent.get(intent)
        if agent_id:
            return self._agents[agent_id]
        return None

    def get_execution_mode(self, intent: str) -> Optional[ExecutionMode]:
        """Return the execution mode for this intent, or None if unknown."""
        descriptor = self.get_agent_for_intent(intent)
        if descriptor:
            return descriptor.capabilities[intent]
        return None

    def get_available_intents(self) -> List[Dict[str, str]]:
        """
        Return all non-internal intents formatted for agent tool declarations.

        Format: [{"name": "search_memory", "description": "..."}, ...]
        Uses per-intent description when available, falls back to agent description.
        Auto-updates whenever new agents are registered.

        Internal agents (internal=True) are excluded — their intents are
        implementation details not shown to LLMs.
        """
        result = []
        for agent_id, descriptor in self._agents.items():
            if descriptor.internal:
                continue
            for intent in descriptor.capabilities:
                description = (
                    descriptor.capability_descriptions.get(intent)
                    or descriptor.description
                )
                result.append({"name": intent, "description": description})
        return result

    def get_available_intents_for(self, descriptor: AgentDescriptor) -> List[Dict[str, str]]:
        """
        Return intents available to a specific orchestrator agent.

        If descriptor.allowed_intents is None → all non-internal intents.
        If descriptor.allowed_intents is a frozenset → filtered subset.

        Intent remapping (descriptor.intent_remap) is NOT applied here —
        it is applied at dispatch time in the calling agent.
        """
        all_intents = self.get_available_intents()
        if descriptor.allowed_intents is None:
            return all_intents
        return [i for i in all_intents if i["name"] in descriptor.allowed_intents]

    def list_agents(self) -> List[AgentDescriptor]:
        """Return all registered descriptors."""
        return list(self._agents.values())
