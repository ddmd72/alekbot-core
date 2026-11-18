"""
Agent Factory Port
==================

Abstraction for on-demand agent instantiation. Used by AgentCoordinator
to lazy-create agents without depending on the composition layer.
"""

from abc import ABC, abstractmethod


class AgentFactoryPort(ABC):
    """
    Port for lazy agent creation.

    The coordinator calls this when a delegation targets a non-eager agent
    that hasn't been instantiated yet. The implementation creates the agent,
    registers it with the coordinator, and returns success/failure status.
    """

    @abstractmethod
    async def create_agent_on_demand(self, agent_type: str, user_id: str) -> bool:
        """
        Create and register a lazy agent for the given user.

        Returns True if the agent was created (or already existed).
        Returns False if the agent could not be created (missing dependencies).
        """
        ...

    async def restore_agents_for_user(self, user_id: str) -> bool:
        """
        Rebuild and register the user's eager agents when they are not registered.

        The coordinator calls this when a message is routed to a user's eager
        agent that is not registered: an eviction (a cache reset, the TTL sweep)
        unregistered the user's agents while a turn was still delegating.
        Returns True if the agents are in service afterwards. A factory that
        builds no eager agents keeps this default and returns False.
        """
        return False
