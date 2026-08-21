"""
SmartRetryService — the Smart-timeout-retry capability, end to end.

Two halves, one owner:
  schedule() — called synchronously from AgentFallbackService.try_quick_fallback
               on AgentStatus.TIMEOUT. Fire-and-forget enqueue, best-effort.
  execute()  — called from WorkerHandler on task_type="smart_timeout_retry".
               Runs Smart directly (bypassing Router) with a fresh budget, and
               delivers a follow-up on success. Silent drop on failure — always
               returns 200 (see execute()'s own docstring for why a failure here
               must not propagate as a non-2xx).

Splitting "when to retry" from "how to retry" across two unrelated layers, connected
only by a task_type string, was the first draft's mistake — this file exists so both
halves share the same AgentMessage shape and the same retry-framing note.
"""
from typing import Any, Dict, List, Optional, Protocol, Tuple

from ..domain.agent import AgentMessage, AgentIntent, AgentStatus, AgentResponse
from ..domain.llm import MessagePart
from ..domain.messaging import MessageContext
from ..utils.logger import logger


# Same REQ-ARCH-22 reasoning as AgentFallbackService's MessageRouter Protocol —
# services/ may not import other services/ directly, so cross-service collaborators
# are duck-typed structurally instead of imported concretely.
class TaskDispatcher(Protocol):
    """Protocol for scheduling background worker tasks. Implemented by TaskDispatchService."""

    async def enqueue_worker_task(
        self,
        task_type: str,
        payload: dict,
        delay_seconds: int = 0,
        deadline_seconds: Optional[int] = None,
        dedup_key: Optional[str] = None,
    ) -> Optional[str]: ...


class MessageRouter(Protocol):
    """Protocol for routing agent messages. Implemented by AgentCoordinator."""

    async def route_message(self, message: AgentMessage) -> AgentResponse: ...


class NotificationTextPort(Protocol):
    """Protocol for delivering a composed answer + persisting it to history.
    Implemented by UserNotificationService.notify_text."""

    async def notify_text(
        self,
        user_id: str,
        account_id: str,
        text: str,
        session_id: Optional[str] = None,
        channel_id_override: Optional[str] = None,
        platform_override: Optional[str] = None,
    ) -> None: ...


class SmartRetryService:
    TASK_TYPE = "smart_timeout_retry"
    _DEADLINE_S = 360  # SmartAgentConfig.timeout_ms (300s) + buffer

    _RETRY_NOTE = (
        "[System: Your previous attempt at this exact request ran out of time before "
        "you could finish — you are being given a second, unhurried attempt at the "
        "same request. A faster, lighter answer from another part of you may already "
        "have reached the user in the meantime; your answer supersedes it with a "
        "fuller one. If you succeed this time, open your reply by naturally telling "
        "the user, in your own voice, that you took your time and thought it through "
        "carefully, and now want to share what you found. Do NOT mention timeouts, "
        "errors, retries, or technical details.]"
    )

    def __init__(
        self,
        task_dispatch: Optional[TaskDispatcher],
        coordinator: MessageRouter,
        notification: NotificationTextPort,
    ) -> None:
        self._task_dispatch = task_dispatch
        self._coordinator = coordinator
        self._notification = notification

    async def schedule(
        self, context: MessageContext, message_parts: List[MessagePart],
    ) -> None:
        """Fire-and-forget one background Smart retry after a timeout.

        dedup_key=session_id caps this at one retry in flight per session — Cloud
        Tasks silently absorbs a second same-session enqueue while the first is
        still queued/running/recently completed (see TaskQueue.enqueue_worker_task).
        Passed raw (unhashed) here — turning it into a valid Cloud Tasks task name
        (hashing, length/charset rules) is the adapter's job (GcpTaskQueue), not
        this service's; this layer only picks which identity the dedup applies to.

        Best-effort: any failure here (queue down, etc.) must never break the
        synchronous Quick fallback path this is called from.
        """
        if self._task_dispatch is None:
            return
        try:
            await self._task_dispatch.enqueue_worker_task(
                task_type=self.TASK_TYPE,
                payload={
                    "user_id": context.user_id,
                    "account_id": context.account_id,
                    "session_id": context.session_id,
                    "thread_id": context.thread_id,
                    "text": context.text or "",
                    "message_parts": [p.model_dump() for p in message_parts],
                },
                deadline_seconds=self._DEADLINE_S,
                dedup_key=context.session_id,
            )
            logger.info(
                "[SmartRetryService] Scheduled background Smart retry for user=%s",
                context.user_id[:8],
            )
        except Exception as exc:
            logger.warning(
                "[SmartRetryService] Failed to schedule Smart retry for user=%s: %s",
                context.user_id[:8], exc,
            )

    async def execute(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        """Run the one retry and deliver a follow-up on success.

        Always returns 200, and nothing in this method may raise past its own
        boundary — the agent-tasks queue retries a non-2xx response up to 3 times
        (retry_config.max_attempts=3), so an uncaught exception or a non-2xx here
        would turn one Smart timeout into up to 4 Smart runs. Everything from the
        required-fields guard onward (payload parsing, AgentMessage construction,
        routing, delivery) is wrapped in a catch-all — a failure anywhere in that
        chain is a silent drop, not an error worth GCP retrying.
        """
        user_id = payload.get("user_id")
        account_id = payload.get("account_id")
        session_id = payload.get("session_id")
        if not user_id or not account_id or not session_id:
            logger.warning(
                "[SmartRetryService] execute: missing user_id/account_id/session_id"
            )
            return {"error": "missing required fields"}, 200

        try:
            thread_id = payload.get("thread_id")
            text = payload.get("text", "")
            raw_parts = payload.get("message_parts", [])
            message_parts = [MessagePart(**p) for p in raw_parts]
            message_parts.append(MessagePart(text=self._RETRY_NOTE))

            message = AgentMessage.create(
                sender="worker",
                recipient=f"smart_response_agent_{user_id}",
                intent=AgentIntent.QUERY,
                payload={"text": text, "attachments": []},
                context={
                    "user_id": user_id,
                    "account_id": account_id,
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "current_message_parts": message_parts,
                },
            )

            try:
                response = await self._coordinator.route_message(message)
            except Exception as exc:
                logger.warning(
                    "[SmartRetryService] execute raised for user=%s: %s", user_id[:8], exc,
                )
                return {"status": "retry_raised"}, 200

            if response.status != AgentStatus.SUCCESS:
                logger.info(
                    "[SmartRetryService] retry did not succeed (status=%s) for user=%s "
                    "— dropping silently", response.status, user_id[:8],
                )
                return {"status": "retry_not_delivered"}, 200

            result = response.result
            # V1 scope: only .text is delivered here. link_list/structured_data on a
            # SmartResponse result (citation anchors, rich content) are dropped by
            # design — notify_text is plain-text-only. A retried answer that cites
            # sources will show literal unresolved [N] anchors to the user. Wiring
            # rich delivery through the retry path is out of scope for this task.
            #
            # `result.text` can be None even when `result` has a `text` attribute
            # (SmartResponse is a plain dataclass with no runtime validation, and an
            # explicit JSON `null` from the LLM response reaches it unguarded — see
            # smart_response_agent.py). Treat a present-but-None text the same as a
            # missing one: both fall through to the empty-response branch below.
            if hasattr(result, "text"):
                response_text = result.text or ""
            else:
                response_text = str(result or "")

            if not response_text.strip():
                logger.info(
                    "[SmartRetryService] retry succeeded but produced empty text for user=%s",
                    user_id[:8],
                )
                return {"status": "retry_empty"}, 200

            await self._notification.notify_text(
                user_id=user_id, account_id=account_id, text=response_text, session_id=session_id,
            )
            logger.info(
                "[SmartRetryService] delivered follow-up for user=%s", user_id[:8],
            )
            return {"status": "delivered"}, 200
        except Exception as exc:
            logger.warning(
                "[SmartRetryService] execute: unexpected error for user=%s: %s",
                user_id[:8], exc,
            )
            return {"status": "unexpected_error"}, 200
