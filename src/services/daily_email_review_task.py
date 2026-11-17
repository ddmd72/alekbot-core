"""The `daily_email_review` `/worker` task: one user's review of the last 24 h of mail, delivered to SmartAgent."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Tuple

from ..domain.exceptions import OAuthGrantRevokedError
from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import empty_answer_alert, is_empty_answer
from ..ports.alert_sink import AlertSinkPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .email_review_service import EmailReviewService
    from .gmail_reconnect_flag import GmailReconnectFlag
    from .user_notification_service import UserNotificationService


class DailyEmailReviewTask:
    """Fetches one user's recent mail and delivers it through `notify()`; a `WorkerTask`."""

    def __init__(
        self,
        *,
        email_review: "EmailReviewService",
        ensure_agents: Callable[[str], Awaitable[Any]],
        notification_service: "UserNotificationService",
        gmail_reconnect_flag: "GmailReconnectFlag",
        alert_sink: Optional[AlertSinkPort],
    ) -> None:
        self._email_review = email_review
        self._ensure_agents = ensure_agents
        self._notification = notification_service
        self._gmail_reconnect_flag = gmail_reconnect_flag
        self._alert_sink = alert_sink

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        """
        Fetch last 24h emails for a user and deliver to SmartAgent for analysis.
        SmartAgent produces an HTML page via create_html_page.
        """
        user_id = payload.get("user_id")
        account_id = payload.get("account_id")
        if not user_id or not account_id:
            return {"error": "missing user_id or account_id"}, 400

        try:
            emails = await self._email_review.fetch_review_payload(user_id)
        except OAuthGrantRevokedError:
            await self._gmail_reconnect_flag.flag(user_id, account_id)
            return {"error": "gmail_reconnect_required"}, 200
        if emails is None:
            return {"error": "credentials unavailable"}, 200
        if not emails:
            logger.info(f"[Worker] daily_email_review: no emails in last 24h for {user_id[:8]}")
            return {"status": "no_emails"}, 200

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        system_alert = self._email_review.build_alert(date_str, len(emails))

        await self._ensure_agents(user_id)
        result = await self._notification.notify(
            kind=NotificationKind.DAILY_DIGEST,
            user_id=user_id,
            account_id=account_id,
            system_alert=system_alert,
            agent_id_override=f"smart_response_agent_{user_id}",
            save_history=False,
            framing_suffix="",
            thinking_effort="medium",
            task_complexity="deep_reasoning",
            email_for_triage=emails,
            # Any HTML page the agent generates in this run is the email review →
            # file it under email_review/ (gated, 5-day TTL).
            storage_class="email_review",
            # 5xx on failure → Cloud Tasks retries the task; suppress in-process
            # agent retry so the two retry layers don't multiply.
            suppress_transient_retry=True,
            origin_detail="daily_email_review",
        )

        if is_empty_answer(result):
            # Final: the per-date mark (set at fan-out) stays, so the review does not re-run today.
            await self._empty_answer_final(user_id)
            return {"status": "empty_answer_final", "emails": len(emails)}, 200

        if not result.delivered:
            # Returning 5xx allows Cloud Tasks queue-level retry to handle
            # transient failures (channel unavailable, agent timeout, etc.).
            logger.warning(
                f"[Worker] daily_email_review: delivery FAILED for {user_id[:8]} "
                f"(emails={len(emails)}, agent_status={result.agent_status}, "
                f"error={result.error})"
            )
            return {
                "error": result.error or "delivery_failed",
                "agent_status": result.agent_status.value,
                "emails": len(emails),
            }, 500

        logger.info(
            f"[Worker] daily_email_review: delivered {len(emails)} emails to Smart for {user_id[:8]}"
        )
        return {"status": "ok", "emails": len(emails)}, 200

    async def _empty_answer_final(self, user_id: str) -> None:
        """The turn ended with no answer: final, never retried (one ERROR line + one ops alert, no content in either)."""
        path = "daily_email_review"
        logger.error(
            "[Worker] background_empty_answer path=%s note=%s user=%s",
            path, None, user_id[:8],
            extra={"event": "background_empty_answer", "path": path, "note_id": None,
                   "user_id": user_id[:8]},
        )
        if self._alert_sink is None:
            return
        try:
            await self._alert_sink.post(empty_answer_alert(path, user_id, None))
        except Exception as exc:
            logger.warning("[Worker] background_empty_answer alert failed: %s", exc)
