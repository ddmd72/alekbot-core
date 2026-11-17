"""The `deep_research_webhook_event` `/worker` task: deliver one OpenAI deep-research outcome.

The webhook route only enqueues this task; `WorkerHandler` dispatches it here through its task registry.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Tuple

from ..domain import deep_research_notices as dr_notices
from ..utils.logger import logger

if TYPE_CHECKING:
    from .deep_research_notice_sender import DeepResearchNoticeSender
    from ..ports.prompt_content_store import PromptContentStore
    from .task_dispatch_service import TaskDispatchService
    from .user_notification_service import UserNotificationService


class DeepResearchWebhookEventTask:
    """Delivers the report (or a notice) of one finished OpenAI research job; a `WorkerTask`."""

    def __init__(
        self,
        *,
        fetch: Callable[[str], Awaitable[dict]],
        ensure_agents: Callable[[str], Awaitable[Any]],
        prompt_content_store: "Optional[PromptContentStore]",
        task_dispatch: "Optional[TaskDispatchService]",
        media_storage: Any,
        link_service: Optional[Any],
        notification_service: "UserNotificationService",
        notice_sender: DeepResearchNoticeSender,
        deliver_report: Callable[..., Awaitable[bool]],
    ) -> None:
        self._fetch = fetch
        self._ensure_agents = ensure_agents
        self._prompt_content_store = prompt_content_store
        self._task_dispatch = task_dispatch
        self._media_storage = media_storage
        self._link_service = link_service
        self._notification = notification_service
        self._notice_sender = notice_sender
        self._deliver_report = deliver_report

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        """Deliver one OpenAI deep-research outcome: report for a completed job, a notice otherwise.
        The report text is re-read by job id here, so it never travels through Cloud Tasks."""
        event_type = payload.get("event_type") or ""
        job_id = payload.get("job_id") or ""
        user_id = payload.get("user_id") or ""
        account_id = payload.get("account_id") or ""
        if not (event_type and job_id and user_id and account_id):
            return {"error": "missing event_type, job_id, user_id or account_id"}, 400
        session_id = payload.get("session_id") or ""
        origin_channel_id = session_id.split(":", 1)[1] if ":" in session_id else None
        origin_platform = payload.get("origin_platform") or None
        query = payload.get("query") or ""

        try:
            response = await self._fetch(job_id)
        except Exception as exc:
            logger.error("[DeepResearch] response retrieval failed: job=%s error=%s", job_id[:16], exc, exc_info=True)
            return {"error": "retrieval_failed"}, 500
        await self._ensure_agents(user_id)

        if event_type == "response.completed":
            output_text = response.get("output_text") or ""
            # Durable capture BEFORE delivery: research is expensive and the result must survive a failed delivery.
            if self._prompt_content_store is not None:
                await self._prompt_content_store.record_dr_result(
                    output_text=output_text, query=query, user_id=user_id, account_id=account_id,
                    model=response.get("model", ""), provider="openai", source="openai_webhook", job_id=job_id,
                )
            handed_off = await self._deliver_report(
                result_text=output_text,
                user_id=user_id,
                account_id=account_id,
                query=query,
                task_queue=self._task_dispatch,
                session_id=session_id,
                media_storage=self._media_storage,
                link_service=self._link_service,
                channel_id_override=origin_channel_id,
                platform_override=origin_platform,
                failure_notification=self._notification,
                report_dedup_id=job_id,
            )
            if not handed_off:
                # The report deliverer has notified the user (or logged that the notice failed); a retry would upload and notify again.
                logger.error("[DeepResearch] report task not enqueued: job=%s user=%s", job_id[:16], user_id[:8])
                return {"status": "report_not_enqueued"}, 200
            logger.info("[DeepResearch] report delivered: user=%s job=%s", user_id[:8], job_id[:16])
            return {"status": "ok"}, 200

        if event_type == "response.failed":
            error = (response.get("error") or {}).get("message", "unknown error")
            logger.error("[DeepResearch] job failed: job=%s error=%s", job_id[:16], error)
            alert = dr_notices.provider_error_notice(query)
        elif event_type == "response.incomplete":
            reason = (response.get("incomplete_details") or {}).get("reason", "unknown")
            logger.warning("[DeepResearch] job incomplete: job=%s reason=%s", job_id[:16], reason)
            alert = dr_notices.incomplete_notice(query)
        elif event_type == "response.cancelled":
            logger.info("[DeepResearch] job cancelled: job=%s", job_id[:16])
            alert = dr_notices.cancelled_notice(query)
        else:
            logger.error("[DeepResearch] unknown webhook event type %r: job=%s", event_type, job_id[:16])
            return {"error": "unknown event_type"}, 400
        return await self._notice_sender.send(
            user_id, account_id, alert, origin_channel_id, origin_platform, context=f"event={event_type}",
        )
