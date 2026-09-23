"""
AlekGatewayAgent — Alek as a specialist behind `ask_alek` (docs/10_rfcs/VOICE_COMPANION_RFC.md §4.7).

Zero-LLM. Routes the commission to router_agent_{user_id} exactly as ConversationHandler
does: the Router is where enrich_context (RRF memory search) runs, and Smart without it is
Alek without memory. The delegation context is forwarded whole, so `_call_chain` reaches
Smart and the coordinator's cycle guard covers this hop. The primary session is read, never
written: only ConversationHandler writes history.

Reading-shaped answers (links, tables) are copied to chat here, the moment Alek answers
(§4.10 rule 2); the relay never posts to chat.
"""
from typing import TYPE_CHECKING, Dict, List, Optional

from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, AgentStatus
from ..domain.llm import MessagePart
from ..domain.messaging import SmartResponse
from ..domain.retry_policy import NO_RETRY_POLICY
from ..utils.logger import logger
from .base_agent import BaseAgent

if TYPE_CHECKING:
    from ..services.user_notification_service import UserNotificationService


def _commission(query: str, reasoning: Optional[str], call_context: List[Dict[str, str]]) -> str:
    lines = [query, "", "[Asked by Lelik during a phone call with the user."]
    if call_context:
        lines.append("The last exchanges on the call:")
        lines.extend(f"{e.get('role', 'user')}: {e.get('text', '')}" for e in call_context)
    if reasoning:
        lines.append(f"Lelik's note: {reasoning}")
    lines[-1] += "]"
    return "\n".join(lines)


class AlekGatewayAgent(BaseAgent):
    """ask_alek → Router → Smart, with the call's context attached."""

    # A retry re-runs Router + Smart + specialists: double spend, double chat copy.
    RETRY_POLICY = NO_RETRY_POLICY

    def __init__(self, config: AgentConfig, notification_service: "UserNotificationService") -> None:
        super().__init__(config)
        self._notifications = notification_service

    async def can_handle(self, message: AgentMessage) -> bool:
        return message.intent == AgentIntent.QUERY and bool(message.payload.get("query"))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        user_id = message.context.get("user_id")
        account_id = message.context.get("account_id")
        text = _commission(
            message.payload.get("query", ""),
            message.payload.get("reasoning"),
            message.payload.get("call_context") or [],
        )
        routed = AgentMessage.create(
            sender=self.agent_id,
            recipient=f"router_agent_{user_id}",
            intent=AgentIntent.QUERY,
            payload={"text": text, "attachments": []},
            # Smart builds its user turn from current_message_parts only.
            context={**message.context, "current_message_parts": [MessagePart(text=text)]},
            timeout_ms=None,
        )
        response = await self.coordinator.route_message(routed)
        if response.status != AgentStatus.SUCCESS:
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id,
                error=f"Alek did not answer: {response.error}",
            )
        summary_task = (response.metadata or {}).get("response_summary_task")
        if summary_task:
            # Nothing writes history on this path; the summary would be paid for and dropped.
            summary_task.cancel()
        answer = response.result
        if isinstance(answer, SmartResponse) and (answer.link_list or answer.structured_data):
            try:
                await self._notifications.notify_answer_copy(user_id, account_id, answer)
            except Exception as exc:
                # The spoken answer matters more than its chat copy.
                logger.error(f"[AlekGateway] chat copy failed for {(user_id or '')[:8]}: {exc}", exc_info=True)
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id, result=answer)
