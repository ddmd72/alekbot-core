"""One deep-research notice to the owner: the send both deep-research `/worker` tasks end in."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import empty_answer_alert, is_empty_answer
from ..ports.alert_sink import AlertSinkPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .user_notification_service import UserNotificationService


class DeepResearchNoticeSender:
    """Sends a deep-research notice and maps the outcome to the task's HTTP answer."""

    def __init__(self, *, notification_service: "UserNotificationService", alert_sink: Optional[AlertSinkPort]) -> None:
        self._notification = notification_service
        self._alert_sink = alert_sink

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
            await self._empty_answer_final("deep_research_notice", user_id)
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

    async def _empty_answer_final(self, path: str, user_id: str) -> None:
        """A background turn ended with no answer: final, never retried (one ERROR line + one ops alert).

        No content in either; the caller then takes its success path and answers 200."""
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
