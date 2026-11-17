"""One deep-research notice to the owner: the send the `deep_research_failed` `/worker` task ends in."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import is_empty_answer
from ..utils.logger import logger

if TYPE_CHECKING:
    from .background_empty_answer_reporter import BackgroundEmptyAnswerReporter
    from .user_notification_service import UserNotificationService


class DeepResearchNoticeSender:
    """Sends a deep-research notice and maps the outcome to the task's HTTP answer."""

    def __init__(self, *, notification_service: "UserNotificationService", empty_answer_reporter: "BackgroundEmptyAnswerReporter") -> None:
        self._notification = notification_service
        self._empty_answer_reporter = empty_answer_reporter

    async def send(
        self, user_id: str, account_id: str, alert: str,
        origin_channel_id: Optional[str], origin_platform: Optional[str], context: str,
    ) -> Tuple[dict, int]:
        """One deep-research notice; 5xx (Cloud Tasks retries, claim released) only when nothing was sent."""
        result = await self._notification.notify(
            kind=NotificationKind.DEEP_RESEARCH,
            user_id=user_id,
            account_id=account_id,
            system_alert=alert,
            channel_id_override=origin_channel_id,
            platform_override=origin_platform,
            # This handler's 5xx makes Cloud Tasks retry; the agent must not retry inside as well.
            suppress_transient_retry=True,
        )
        if is_empty_answer(result):
            await self._empty_answer_reporter.report("deep_research_notice", user_id)
            return {"status": "empty_answer_final"}, 200
        if not result.delivered and not result.sent:
            logger.warning(
                "[DeepResearch] notice not sent: user=%s %s error=%s", user_id[:8], context, result.error,
            )
            return {"error": result.error or "delivery_failed"}, 500
        if not result.delivered:
            # A resend would double a message that may already be on the channel.
            logger.warning(
                "[DeepResearch] notice sent but not confirmed: user=%s %s error=%s; not retried",
                user_id[:8], context, result.error,
            )
        return {"status": "ok"}, 200
