"""
Agent Registry
==============

Central registry for dynamic agent discovery.
Maps intents to agent manifests with per-intent execution modes.

Enables adding new specialist agents without modifying SmartResponseAgent.
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
class AgentManifest:
    """
    Agent capability declaration.

    Registered once at startup; consumed by AgentRegistry to route
    delegate_to_specialist tool calls from SmartResponseAgent.

    intents: mapping of intent name → execution mode.
    intent_descriptions: optional per-intent descriptions injected into the
        delegate_to_specialist tool declaration. Falls back to agent-level
        description when not provided.
    Example:
        AgentManifest(
            agent_id="gmail_agent",
            intents={
                "search_email": ExecutionMode.SYNC,
                "index_gmail": ExecutionMode.ASYNC,
            },
            intent_descriptions={
                "search_email": "Semantic search across indexed emails",
                "index_gmail": "Trigger background Gmail indexing job",
            },
            description="Gmail integration specialist",
        )
    """
    agent_id: str
    intents: Dict[str, ExecutionMode]
    description: str
    intent_descriptions: Dict[str, str] = field(default_factory=dict)
    requires_auth: bool = False


class AgentRegistry:
    """
    Central registry for agent discovery.

    Stores AgentManifest entries and maps intent names to manifests.
    Used by AgentCoordinator.handle_delegation() to route
    delegate_to_specialist calls without hardcoded maps in SmartAgent.
    """

    def __init__(self) -> None:
        self._agents: Dict[str, AgentManifest] = {}
        self._intent_to_agent: Dict[str, str] = {}  # intent → agent_id

    def register(self, manifest: AgentManifest) -> None:
        """
        Register an agent manifest.

        Overwrites existing registration for the same agent_id.
        Logs a warning if an intent is already claimed by another agent.
        """
        self._agents[manifest.agent_id] = manifest

        for intent in manifest.intents:
            existing = self._intent_to_agent.get(intent)
            if existing and existing != manifest.agent_id:
                logger.warning(
                    f"Intent '{intent}' was registered by '{existing}', "
                    f"overwriting with '{manifest.agent_id}'"
                )
            self._intent_to_agent[intent] = manifest.agent_id

        logger.info(
            f"Registered agent manifest: {manifest.agent_id} "
            f"(intents={list(manifest.intents.keys())})"
        )

    def get_agent_for_intent(self, intent: str) -> Optional[AgentManifest]:
        """Return the manifest for the agent that handles this intent, or None."""
        agent_id = self._intent_to_agent.get(intent)
        if agent_id:
            return self._agents[agent_id]
        return None

    def get_execution_mode(self, intent: str) -> Optional[ExecutionMode]:
        """Return the execution mode for this intent, or None if unknown."""
        manifest = self.get_agent_for_intent(intent)
        if manifest:
            return manifest.intents[intent]
        return None

    def get_available_intents(self) -> List[Dict[str, str]]:
        """
        Return all registered intents formatted for agent tool declarations.

        Format: [{"name": "search_memory", "description": "..."}, ...]
        Uses per-intent description when available, falls back to agent description.
        Auto-updates whenever new agents are registered.
        """
        result = []
        for agent_id, manifest in self._agents.items():
            for intent in manifest.intents:
                description = (
                    manifest.intent_descriptions.get(intent)
                    or manifest.description
                )
                result.append({"name": intent, "description": description})
        return result

    def list_agents(self) -> List[AgentManifest]:
        """Return all registered manifests."""
        return list(self._agents.values())
