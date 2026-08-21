"""
AgentFallbackService — graceful degradation for failed agent responses.

Degradation chain: Smart (FAILED/TIMEOUT) → Quick → synthetic apology text.

Why Quick and not a Smart retry (for genuine FAILED, not TIMEOUT):
- Smart's provider/model is dynamically assembled by the Router. If that assembly
  produced a bad combination, retrying Smart likely fails again for the same reason.
- Quick has a fixed, conservative provider/model config — its failure surface is
  deliberately independent of whatever caused Smart to fail.
- Quick also formulates the apology in the user's language/style via the prompt system
  (a system note is injected so the model knows to apologize gracefully, not expose
  technical details). A raw retry or static string cannot do this.

Why TIMEOUT gets a background Smart retry anyway (2026-08-20, see SmartRetryService):
- A timeout is not a deterministic misconfiguration — it is Smart genuinely still
  working when the fixed request budget ran out (root-caused: SmartAgentConfig.timeout_ms
  bounds the WHOLE delegation loop cumulatively, so a multi-hop research turn can burn
  most of its budget on tool calls before the final synthesis call). Retrying the exact
  same request off the synchronous critical path, with a fresh full budget, is likely to
  succeed — timeouts do not reliably repeat back-to-back.
- Scheduling and running the retry is owned entirely by SmartRetryService — this class
  only decides WHETHER to call it, on AgentStatus.TIMEOUT specifically.
"""
from typing import Any, List, Optional, Protocol

from ..domain.agent import AgentMessage, AgentIntent, AgentStatus, AgentResponse
from ..domain.messaging import MessageContext
from ..ports.llm_port import MessagePart
from ..utils.logger import logger


# ARCHITECTURE FIX: services/ must not import from infrastructure/ or other services/.
# Replaced with structural Protocols (duck-typed at runtime) — same pattern for both
# AgentCoordinator (infrastructure/) and SmartRetryService (services/).
class MessageRouter(Protocol):
    """Protocol for routing agent messages. Implemented by AgentCoordinator."""

    async def route_message(self, message: AgentMessage) -> AgentResponse: ...


class SmartRetrySchedulerPort(Protocol):
    """Protocol for scheduling a background Smart retry. Implemented by SmartRetryService."""

    async def schedule(
        self,
        context: MessageContext,
        message_parts: List[MessagePart],
        origin_platform: Optional[str] = None,
    ) -> bool: ...


class AgentFallbackService:
    """Graceful degradation chain: primary failure → QuickAgent → synthetic apology."""

    # Used when smart_retry.schedule() actually put a retry in flight (returned True) —
    # only then is it honest to let Quick tell the user a fuller answer may follow.
    _TIMEOUT_NOTE = (
        "[System: The primary assistant is still deep in extended reasoning on this "
        "exact request and has not finished within its time budget — this is not a "
        "failure. Open your reply by naturally telling the user, in your own voice, "
        "that another part of you is still thinking it through and you'll give a fast "
        "answer now; you may add that a fuller answer could follow shortly. Do NOT "
        "say you're sorry, and do NOT mention timeouts, errors, or technical details. Then "
        "answer the user's question as well as you can.]"
    )

    # Same fast-lane framing as _TIMEOUT_NOTE, minus the follow-up promise. Used when
    # smart_retry.schedule() returned False (not configured, or Cloud Tasks deduped
    # this against an already in-flight retry for the same session) — nothing is
    # actually going to arrive later, so Quick must not set that expectation.
    _TIMEOUT_NOTE_NO_FOLLOWUP = (
        "[System: The primary assistant is still deep in extended reasoning on this "
        "exact request and has not finished within its time budget — this is not a "
        "failure. Open your reply by naturally telling the user, in your own voice, "
        "that another part of you is still thinking it through and you'll give a fast "
        "answer now. Do NOT say a fuller answer may follow, do NOT say you're sorry, "
        "and do NOT mention timeouts, errors, or technical details. Then answer the "
        "user's question as well as you can.]"
    )

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
        smart_retry: Optional[SmartRetrySchedulerPort] = None,
    ) -> None:
        self._coordinator = coordinator
        # Optional ops webhook (SlackWebhookAdapter.post — async, text). When set, a primary
        # (Smart) failure fires an alert so the silent Quick fallback doesn't mask systematic
        # Smart outages (e.g. a bad provider/model config). See composition wiring (main.py).
        self._alert_webhook = alert_webhook
        # Optional — when set, a TIMEOUT (not generic FAILED) additionally schedules one
        # background Smart retry via SmartRetryService. None in any composition path that
        # hasn't wired it yet; scheduling is skipped gracefully (Quick fallback still runs).
        self._smart_retry = smart_retry

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
        On TIMEOUT: injects a "fast lane" system note (not an apology) into Quick's
        call, and calls smart_retry.schedule() for one background Smart retry with a
        fresh budget. Which fast-lane note is used depends on schedule()'s return —
        _TIMEOUT_NOTE (may promise a follow-up) if a retry was genuinely put in
        flight, _TIMEOUT_NOTE_NO_FOLLOWUP (no such promise) if scheduling was a
        no-op (not configured, or Cloud Tasks deduped it against one already in
        flight for this session) — Quick must never promise a follow-up that isn't
        actually coming. On other FAILED reasons: injects the apology note, no retry.
        If QuickAgent also fails: returns a synthetic SUCCESS with an apology so the
        caller always receives a displayable response.

        origin_platform: passed through to smart_retry.schedule() so a delivered
        retry answer lands on the SAME channel the original request came from.
        Sourced by the caller from response_channel.platform (see
        ConversationHandler) — NOT context.metadata.get("platform"), which is only
        populated on Slack's "$command" path, not the regular message/app_mention
        path a Smart timeout actually happens on.
        """
        if failed_response.status == AgentStatus.SUCCESS:
            return failed_response

        logger.warning(
            "[AgentFallbackService] Primary agent failed (%s), attempting QuickAgent fallback",
            failed_response.status,
        )

        is_timeout = failed_response.status == AgentStatus.TIMEOUT

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

        retry_scheduled = False
        if is_timeout and self._smart_retry is not None:
            retry_scheduled = await self._smart_retry.schedule(
                context, message_parts, origin_platform=origin_platform,
            )

        if not is_timeout:
            note_text = self._FAILURE_NOTE
        elif retry_scheduled:
            note_text = self._TIMEOUT_NOTE
        else:
            note_text = self._TIMEOUT_NOTE_NO_FOLLOWUP
        system_note = MessagePart(text=note_text)
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
