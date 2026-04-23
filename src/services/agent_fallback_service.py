"""
AgentFallbackService — graceful degradation for failed agent responses.

Degradation chain: Smart (FAILED/TIMEOUT) → Quick → synthetic apology text.

Why Quick and not a Smart retry:
- Smart's provider/model is dynamically assembled by the Router. If that assembly
  produced a bad combination, retrying Smart likely fails again for the same reason.
- Quick has a fixed, conservative provider/model config — its failure surface is
  deliberately independent of whatever caused Smart to fail.
- Quick also formulates the apology in the user's language/style via the prompt system
  (_SYSTEM_NOTE is injected so the model knows to apologize gracefully, not expose
  technical details). A raw retry or static string cannot do this.
"""
from typing import List, Protocol

from ..domain.agent import AgentMessage, AgentIntent, AgentStatus, AgentResponse
from ..domain.messaging import MessageContext
from ..ports.llm_port import MessagePart
from ..utils.logger import logger


# ARCHITECTURE FIX: services/ must not import from infrastructure/.
# AgentCoordinator was imported directly — same violation that UserNotificationService
# had before. Replaced with structural Protocol (duck-typed at runtime).
class MessageRouter(Protocol):
    """Protocol for routing agent messages. Implemented by AgentCoordinator."""

    async def route_message(self, message: AgentMessage) -> AgentResponse: ...


class AgentFallbackService:
    """Graceful degradation chain: primary failure → QuickAgent → synthetic apology."""

    _SYSTEM_NOTE = (
        "[System: The assistant attempted extended reasoning for the user's request "
        "but did not complete in time. Briefly acknowledge the delay in the assistant's "
        "voice, apologize concisely, and offer to answer from memory or ask the user "
        "to rephrase the question more simply. Do NOT mention technical details or errors.]"
    )

    # ARCHITECTURE FIX: Was hardcoded in Ukrainian. Last-resort apology must be
    # language-neutral English — the user's language/style is normally applied by
    # QuickAgent via prompt system. This text only appears when ALL agents fail.
    _APOLOGY_TEXT = (
        "I'm sorry, something went wrong on my end. "
        "Please try again or rephrase your question."
    )

    def __init__(self, coordinator: MessageRouter) -> None:
        self._coordinator = coordinator

    async def try_quick_fallback(
        self,
        failed_response: AgentResponse,
        context: MessageContext,
        message_parts: List[MessagePart],
    ) -> AgentResponse:
        """
        Attempt QuickAgent fallback for a failed primary response.

        Returns the original response unchanged if status is SUCCESS.
        On TIMEOUT/FAILED: routes to QuickAgent with a system note injected
        into current_message_parts so the model knows to apologize gracefully.
        If QuickAgent also fails: returns a synthetic SUCCESS with an apology
        so the caller always receives a displayable response.
        """
        if failed_response.status == AgentStatus.SUCCESS:
            return failed_response

        logger.warning(
            "[AgentFallbackService] Primary agent failed (%s), attempting QuickAgent fallback",
            failed_response.status,
        )

        system_note = MessagePart(text=self._SYSTEM_NOTE)
        fallback_message = AgentMessage.create(
            sender="agent_fallback_service",
            recipient=f"quick_response_agent_{context.user_id}",
            intent=AgentIntent.QUERY,
            payload={"text": context.text or ""},
            context={
                "session_id": context.session_id,
                "user_id": context.user_id,
                "account_id": context.account_id,
                "thread_id": context.thread_id,
                "current_message_parts": list(message_parts) + [system_note],
            },
        )

        try:
            response = await self._coordinator.route_message(fallback_message)
            if response.status == AgentStatus.SUCCESS:
                logger.warning("[AgentFallbackService] QuickAgent fallback succeeded")
                return response
            logger.warning(
                "[AgentFallbackService] QuickAgent fallback also failed (%s)",
                response.status,
            )
        except Exception as exc:
            logger.warning("[AgentFallbackService] QuickAgent fallback raised: %s", exc)

        logger.warning("[AgentFallbackService] Returning synthetic apology response")
        return AgentResponse.success(
            task_id=failed_response.task_id,
            agent_id="agent_fallback_service",
            result=self._APOLOGY_TEXT,
        )
