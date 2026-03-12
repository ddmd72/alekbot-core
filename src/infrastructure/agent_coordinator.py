"""
Agent Coordinator
=================

Central routing and coordination service for multi-agent architecture.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Dict, List, Optional, Any
from ..domain.agent import AgentMessage, AgentResponse, AgentStatus, AgentIntent
from ..utils.logger import logger
from .agent_registry import AgentRegistry, AgentDescriptor, ExecutionMode
from ..ports.task_queue import TaskQueue

if TYPE_CHECKING:
    from ..agents.base_agent import BaseAgent


class AgentCoordinator:
    """
    Coordinator for agent communication and routing.

    Responsibilities:
    - Register and manage agents
    - Route messages to appropriate agents (ACP v1: route_message)
    - Handle delegate_to_specialist calls from SmartAgent (ACP v2: handle_delegation)
    - Execute agents in parallel
    - Provide agent discovery
    """

    def __init__(
        self,
        registry: Optional[AgentRegistry] = None,
        task_queue: Optional[TaskQueue] = None,
    ):
        """
        Initialize coordinator.

        Args:
            registry: AgentRegistry for v2 intent-based routing.
                      If None, handle_delegation() will always return failure.
            task_queue: TaskQueue port for async intent execution.
                        Required when any registered intent uses ExecutionMode.ASYNC.
        """
        self.agents: Dict[str, BaseAgent] = {}
        self._registry = registry
        self._task_queue = task_queue
        logger.info("🎯 AgentCoordinator initialized")
    
    def register_agent(self, agent: BaseAgent) -> None:
        """
        Register an agent with the coordinator.
        
        Args:
            agent: Agent instance to register
            
        Raises:
            ValueError: If agent_id already registered
        """
        if agent.agent_id in self.agents:
            raise ValueError(
                f"Agent {agent.agent_id} is already registered. "
                "Use different agent_id or unregister first."
            )
        
        self.agents[agent.agent_id] = agent
        logger.info(
            f"✅ Registered agent: {agent.agent_id} "
            f"(type={agent.agent_type}, capabilities={agent.config.capabilities})"
        )
    
    def unregister_agent(self, agent_id: str) -> bool:
        """
        Unregister an agent.
        
        Args:
            agent_id: ID of agent to unregister
            
        Returns:
            True if agent was unregistered, False if not found
        """
        if agent_id in self.agents:
            del self.agents[agent_id]
            logger.info(f"↩️ Unregistered agent: {agent_id}")
            return True
        return False
    
    def get_agent(self, agent_id: str) -> Optional[BaseAgent]:
        """
        Get agent by ID.
        
        Args:
            agent_id: Agent identifier
            
        Returns:
            Agent instance or None if not found
        """
        return self.agents.get(agent_id)
    
    def list_agents(self) -> List[str]:
        """
        List all registered agent IDs.
        
        Returns:
            List of agent identifiers
        """
        return list(self.agents.keys())
    
    def get_agents_by_capability(self, capability: str) -> List[BaseAgent]:
        """
        Find agents with specific capability.
        
        Args:
            capability: Capability to search for
            
        Returns:
            List of agents with this capability
        """
        return [
            agent for agent in self.agents.values()
            if capability in agent.config.capabilities
        ]
    
    async def route_message(self, message: AgentMessage) -> AgentResponse:
        """
        Route message to appropriate agent.
        
        Routing strategies:
        1. Explicit routing: If recipient is specific agent_id
        2. Broadcast routing: If recipient is "broadcast", find capable agents
        3. Fallback: Return error if no route found
        
        Args:
            message: Agent message to route
            
        Returns:
            Agent response
        """
        logger.info(
            f"📨 Routing message {message.task_id[:8]} "
            f"from {message.sender} to {message.recipient} "
            f"(intent={message.intent})"
        )
        
        # Strategy 1: Explicit routing
        if message.recipient in self.agents:
            agent = self.agents[message.recipient]
            logger.info(
                f"🎯 [AgentCoordinator] Found agent {agent.agent_id} in registry, "
                f"calling process()..."
            )
            
            try:
                response = await agent.process(message)
                logger.info(
                    f"✅ [AgentCoordinator] Agent {agent.agent_id} returned: "
                    f"status={response.status}, confidence={response.confidence:.2f}"
                )
                return response
            except Exception as e:
                logger.error(
                    f"❌ [AgentCoordinator] Agent {agent.agent_id} raised exception: {e}",
                    exc_info=True
                )
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=agent.agent_id,
                    error=f"Agent execution failed: {str(e)}"
                )
        
        # Strategy 2: Broadcast routing (intent-based)
        if message.recipient == "broadcast":
            return await self._broadcast_route(message)
        
        # Strategy 3: No route found
        logger.error(f"❌ No route found for recipient: {message.recipient}")
        return AgentResponse.failure(
            task_id=message.task_id,
            agent_id="coordinator",
            error=f"Unknown recipient: {message.recipient}",
            suggestions=self.list_agents()
        )
    
    async def _broadcast_route(self, message: AgentMessage) -> AgentResponse:
        """
        Find and route to capable agent via broadcast.
        
        Args:
            message: Agent message
            
        Returns:
            Agent response from first capable agent
        """
        logger.debug("🔍 Searching for capable agents...")
        
        # Find all agents that can handle this message
        capable_agents = []
        
        for agent in self.agents.values():
            try:
                if await agent.can_handle(message):
                    capable_agents.append(agent)
                    logger.debug(f"✓ {agent.agent_id} can handle this message")
            except Exception as e:
                logger.warning(f"Error checking {agent.agent_id}.can_handle(): {e}")
        
        if not capable_agents:
            logger.warning("⚠️ No capable agents found for broadcast")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id="coordinator",
                error="No agent can handle this task",
                suggestions=self._suggest_agents(message)
            )
        
        # Select best agent (for now, just take first)
        # TODO: Implement smarter selection (priority, load balancing, etc.)
        selected_agent = capable_agents[0]
        
        if len(capable_agents) > 1:
            logger.info(
                f"🎯 Multiple capable agents found. Selected: {selected_agent.agent_id} "
                f"(alternatives: {[a.agent_id for a in capable_agents[1:]]})"
            )
        else:
            logger.info(f"🎯 Selected agent: {selected_agent.agent_id}")
        
        return await selected_agent.process(message)
    
    async def parallel_execute(
        self, 
        messages: List[AgentMessage],
        return_exceptions: bool = True
    ) -> List[AgentResponse]:
        """
        Execute multiple agent tasks in parallel.
        
        Args:
            messages: List of agent messages to process
            return_exceptions: If True, exceptions are returned as AgentResponse.failure
            
        Returns:
            List of agent responses (same order as messages)
        """
        logger.info(f"⚡ Parallel execution of {len(messages)} tasks")
        
        # Create tasks for all messages
        tasks = [self.route_message(msg) for msg in messages]
        
        # Execute in parallel
        start_time = asyncio.get_event_loop().time()
        results = await asyncio.gather(*tasks, return_exceptions=return_exceptions)
        elapsed = asyncio.get_event_loop().time() - start_time
        
        logger.info(f"✅ Parallel execution completed in {elapsed:.2f}s")
        
        # Convert exceptions to failure responses if needed
        if return_exceptions:
            processed_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    processed_results.append(
                        AgentResponse.failure(
                            task_id=messages[i].task_id,
                            agent_id="coordinator",
                            error=f"Parallel execution exception: {str(result)}"
                        )
                    )
                else:
                    processed_results.append(result)
            return processed_results
        
        return results
    
    def _suggest_agents(self, message: AgentMessage) -> List[str]:
        """
        Suggest alternative agents based on message intent.
        
        Args:
            message: Agent message
            
        Returns:
            List of suggested agent IDs
        """
        # Simple heuristic: suggest agents based on intent
        intent_to_agent_type = {
            AgentIntent.QUERY: ["memory_search", "web_search"],
            AgentIntent.DELEGATE: ["observation", "consolidation"],
        }
        
        target_types = intent_to_agent_type.get(message.intent, [])
        
        suggestions = [
            agent.agent_id for agent in self.agents.values()
            if agent.agent_type in target_types
        ]
        
        return suggestions if suggestions else self.list_agents()
    
    # ------------------------------------------------------------------ #
    # ACP v2: Intent-based delegation (used by SmartResponseAgent)        #
    # ------------------------------------------------------------------ #

    async def handle_delegation(
        self,
        intent: str,
        query: str,
        context: Dict[str, Any],
        calling_agent_id: str = "unknown",
    ) -> AgentResponse:
        """
        Handle a delegate_to_specialist call from SmartResponseAgent.

        Looks up intent in AgentRegistry, then routes:
        - SYNC  → _execute_sync (immediate, returns result)
        - ASYNC → _execute_async (enqueue Cloud Tasks, returns ack)

        Args:
            intent:           Intent name (e.g. "search_memory", "index_gmail")
            query:            User query or command string
            context:          Must contain user_id. May contain extra params
                              under "params" key that are spread into AgentMessage.payload.
            calling_agent_id: For logging only.
        """
        if self._registry is None:
            logger.error("handle_delegation called but no AgentRegistry configured")
            return AgentResponse.failure(
                task_id="delegation",
                agent_id="coordinator",
                error="AgentRegistry not configured"
            )

        manifest = self._registry.get_agent_for_intent(intent)
        if not manifest:
            logger.warning(f"Unknown intent '{intent}' requested by {calling_agent_id}")
            return AgentResponse.failure(
                task_id="delegation",
                agent_id="coordinator",
                error=f"No agent registered for intent: {intent}",
                suggestions=[i["name"] for i in self._registry.get_available_intents()]
            )

        mode = manifest.capabilities[intent]
        logger.info(
            f"Delegating intent='{intent}' to agent='{manifest.agent_id}' "
            f"mode={mode} (from {calling_agent_id})"
        )

        if mode == ExecutionMode.SYNC:
            return await self._execute_sync(manifest.agent_id, intent, query, context)
        else:
            return await self._execute_async(manifest.agent_id, intent, query, context, manifest.dispatch_deadline_s)

    async def _execute_sync(
        self,
        base_agent_id: str,
        intent: str,
        query: str,
        context: Dict[str, Any],
    ) -> AgentResponse:
        """Route SYNC intent to the per-user agent instance via route_message."""
        user_id = context.get("user_id", "")
        agent_id = f"{base_agent_id}_{user_id}" if user_id else base_agent_id

        # Extra params from SmartAgent's delegate_to_specialist context.params field
        extra_payload = context.get("params", {})

        message = AgentMessage.create(
            sender="coordinator",
            recipient=agent_id,
            intent=AgentIntent.QUERY,
            payload={"query": query, "intent": intent, **extra_payload},
            context={k: v for k, v in context.items() if k != "params"},
        )
        return await self.route_message(message)

    async def _execute_async(
        self,
        base_agent_id: str,
        intent: str,
        query: str,
        context: Dict[str, Any],
        deadline_seconds: Optional[int] = None,
    ) -> AgentResponse:
        """Enqueue ASYNC intent to Cloud Tasks, return immediate ack."""
        if self._task_queue is None:
            logger.error(
                f"ASYNC intent '{intent}' requested but no TaskQueue configured"
            )
            return AgentResponse.failure(
                task_id="delegation",
                agent_id="coordinator",
                error="TaskQueue not configured for async execution"
            )

        task_name = await self._task_queue.enqueue_agent_task(
            agent_id=base_agent_id,
            intent=intent,
            query=query,
            context=context,
            deadline_seconds=deadline_seconds,
        )

        return AgentResponse.success(
            task_id="delegation",
            agent_id="coordinator",
            result={
                "status": "started",
                "task_name": task_name,
                "message": "Task started in background. You will be notified when complete.",
            },
        )

    def get_available_intents(self) -> List[Dict[str, str]]:
        """
        Return available intents from AgentRegistry for agent tool declarations.
        Returns [] if registry is not configured.
        """
        if self._registry is None:
            return []
        return self._registry.get_available_intents()

    def get_available_intents_for(self, descriptor: AgentDescriptor) -> List[Dict[str, str]]:
        """
        Return intents available to a specific orchestrator agent.

        Filters by descriptor.allowed_intents (None = all non-internal).
        Intent remapping is NOT applied here — handled at dispatch time.
        Returns [] if registry is not configured.
        """
        if self._registry is None:
            return []
        return self._registry.get_available_intents_for(descriptor)

    # ------------------------------------------------------------------ #
    # Monitoring                                                           #
    # ------------------------------------------------------------------ #

    def get_status(self) -> Dict[str, any]:
        """
        Get coordinator status for monitoring.
        
        Returns:
            Status dictionary with all agents
        """
        return {
            "total_agents": len(self.agents),
            "agents": {
                agent_id: agent.get_status()
                for agent_id, agent in self.agents.items()
            }
        }
