"""
Agent Worker Handler
====================

Handles async agent task execution triggered by Cloud Tasks.
Receives payloads with task_type="agent_execution", executes the
specified agent, and logs the result.

Notification of the user (via Slack, Telegram, etc.) is intentionally
deferred — it will be added when the first real async agent (Gmail) ships.
"""

from typing import Dict, Any, Optional

from ..domain.agent import AgentMessage, AgentIntent, AgentStatus
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..utils.logger import logger


class AgentWorkerHandler:
    """
    Background task executor for async agent intents.

    Invoked by the /worker HTTP endpoint when Cloud Tasks delivers a
    payload with task_type="agent_execution".

    Responsibilities (MVP):
    - Resolve agent instance from coordinator
    - Execute agent with the original query + context
    - Log result (success or failure)

    Future (when Gmail agent ships):
    - Notify user via ResponseChannel after completion
    """

    def __init__(self, coordinator: AgentCoordinator) -> None:
        self._coordinator = coordinator

    async def handle_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute an async agent task.

        Expected payload shape:
        {
            "task_type": "agent_execution",
            "agent_id": "gmail_agent",          # base agent_id (without user_id suffix)
            "intent":   "index_gmail",
            "query":    "index all emails",
            "context":  {"user_id": "...", "account_id": "...", ...}
        }

        Returns a result dict (used by the HTTP endpoint for the response body).
        """
        agent_id = payload.get("agent_id", "unknown")
        intent = payload.get("intent", "unknown")
        query = payload.get("query", "")
        context = payload.get("context", {})

        user_id = context.get("user_id", "")
        resolved_agent_id = f"{agent_id}_{user_id}" if user_id else agent_id

        logger.info(
            f"[AgentWorkerHandler] Executing: agent={resolved_agent_id}, "
            f"intent={intent}, user={user_id}"
        )

        message = AgentMessage.create(
            sender="worker",
            recipient=resolved_agent_id,
            intent=AgentIntent.DELEGATE,
            payload={"query": query, "intent": intent},
            context=context,
        )

        try:
            response = await self._coordinator.route_message(message)

            if response.status == AgentStatus.SUCCESS:
                logger.info(
                    f"[AgentWorkerHandler] Task completed: agent={resolved_agent_id}, "
                    f"intent={intent}"
                )
                # TODO: notify user via platform-agnostic ResponseChannel
                return {"status": "success", "agent_id": resolved_agent_id, "intent": intent}

            else:
                logger.error(
                    f"[AgentWorkerHandler] Task failed: agent={resolved_agent_id}, "
                    f"intent={intent}, status={response.status}, error={response.error}"
                )
                # TODO: notify user of failure via platform-agnostic ResponseChannel
                return {
                    "status": "failed",
                    "agent_id": resolved_agent_id,
                    "intent": intent,
                    "error": response.error,
                }

        except Exception as e:
            logger.error(
                f"[AgentWorkerHandler] Unexpected error: agent={resolved_agent_id}, "
                f"intent={intent}, error={e}",
                exc_info=True,
            )
            raise
