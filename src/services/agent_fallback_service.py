"""
AgentFallbackService — graceful degradation for failed agent responses.

Degradation chain: Smart (FAILED/TIMEOUT) → Quick → synthetic apology text.

Why Quick and not a Smart retry:
- Smart's provider/model is dynamically assembled by the Router. If that assembly
  produced a bad combination, retrying Smart likely fails again for the same reason.
- Quick has a fixed, conservative provider/model config — its failure surface is
  deliberately independent of whatever caused Smart to fail.
- Quick also formulates the apology in the user's language/style via the prompt system
  (a system note is injected so the model knows to apologize gracefully, not expose
  technical details). A raw retry or static string cannot do this.

TIMEOUT is reported like any other failure (2026-10-05, retiring the two-phase
timeout path — LONG_RUNNING_TURNS_RFC §5.9). Under the long-running-turns budget
(~25 min wall clock) a Smart call only times out after the long-turn mark, where
the run's own failure/retry handling (RFC §5.2, §5.7) owns the outcome — a
synchronous background Smart retry scheduled from here would fire after the turn
has already been reported, promising a follow-up that never arrives. Before the
mark, a TIMEOUT is simply a FAILED-shaped response and gets the same apology note
as any other failure.
"""
from typing import Any, List, Optional, Protocol

from ..domain.agent import AgentMessage, AgentIntent, AgentStatus, AgentResponse
from ..domain.messaging import MessageContext
from ..ports.llm_port import MessagePart
from ..utils.logger import logger


# ARCHITECTURE FIX: services/ must not import from infrastructure/ or other services/.
# Replaced with a structural Protocol (duck-typed at runtime) — same pattern used for
# AgentCoordinator (infrastructure/).
class MessageRouter(Protocol):
    """Protocol for routing agent messages. Implemented by AgentCoordinator."""

    async def route_message(self, message: AgentMessage) -> AgentResponse: ...


class AgentFallbackService:
    """Graceful degradation chain: primary failure → QuickAgent → synthetic apology."""

    _FAILURE_NOTE = (
        "[System: The assistant ran into a problem and could not finish this request. "
        "Briefly acknowledge that in the assistant's voice, apologize concisely, and "
        "offer to answer from memory or ask the user to rephrase the question more "
        "simply. Do NOT mention technical details or errors.]"
    )

    # ARCHITECTURE FIX: Was hardcoded in Ukrainian. Last-resort apology must be
    # language-neutral English — the user's language/style is normally applied by
    # QuickAgent via prompt system. This text only appears when ALL agents fail.
    _APOLOGY_TEXT = (
        "I'm sorry, something went wrong on my end. "
        "Please try again or rephrase your question."
    )

    def __init__(
        self,
        coordinator: MessageRouter,
        alert_webhook: Optional[Any] = None,
    ) -> None:
        self._coordinator = coordinator
        # Optional ops webhook (SlackWebhookAdapter.post — async, text). When set, a primary
        # (Smart) failure fires an alert so the silent Quick fallback doesn't mask systematic
        # Smart outages (e.g. a bad provider/model config). See composition wiring (main.py).
        self._alert_webhook = alert_webhook

    async def try_quick_fallback(
        self,
        failed_response: AgentResponse,
        context: MessageContext,
        message_parts: List[MessagePart],
        origin_platform: Optional[str] = None,
    ) -> AgentResponse:
        """
        Attempt QuickAgent fallback for a failed primary response.

        Returns the original response unchanged if status is SUCCESS.
        Any other status — FAILED or TIMEOUT alike — gets the same apology note
        injected into Quick's call (LONG_RUNNING_TURNS_RFC §5.9: under the
        long-running-turns budget a Smart call only times out after the long-turn
        mark, where the run's own outcome handling owns it; before the mark a
        TIMEOUT is just a failure like any other, with no follow-up to promise).
        If QuickAgent also fails: returns a synthetic SUCCESS with an apology so the
        caller always receives a displayable response.

        origin_platform: accepted for call-site signature stability (callers source
        it from response_channel.platform — see ConversationHandler) but currently
        unused inside this method; kept so a future per-platform fallback need does
        not require touching every call site again.
        """
        if failed_response.status == AgentStatus.SUCCESS:
            return failed_response

        logger.warning(
            "[AgentFallbackService] Primary agent failed (%s), attempting QuickAgent fallback",
            failed_response.status,
        )

        # Surface primary (Smart) failures to ops — otherwise the Quick fallback masks them and
        # all traffic silently looks like it's on Quick (incident 2026-07-13: a mis-cased provider
        # made Smart fail 100%). Best-effort; never let alerting break the degradation path.
        if self._alert_webhook is not None:
            try:
                detail = (failed_response.error or "")[:400]
                await self._alert_webhook.post(
                    f"⚠️ Smart primary FAILED ({failed_response.status.value}) → Quick fallback. "
                    f"user={context.user_id[:8]} detail: {detail or 'n/a'}"
                )
            except Exception as exc:
                logger.warning("[AgentFallbackService] primary-failure alert failed: %s", exc)

        system_note = MessagePart(text=self._FAILURE_NOTE)
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
                logger.info("[AgentFallbackService] QuickAgent fallback succeeded")
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
