"""The `video_generation_polling` `/worker` task: poll one video job, re-enqueue while pending, deliver or notify.

Kicked off by `GrokVideoAdapter`; `WorkerHandler` dispatches it here through its task registry (built by
`composition/worker_setup.py`). VIDEO_GENERATION_RFC.md §3.4 step 4.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Tuple

from ..domain.notification_kind import NotificationKind
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..ports.video_generation_port import VideoPollResult
    from .provider_registry import ProviderRegistry
    from .task_dispatch_service import TaskDispatchService
    from .user_notification_service import UserNotificationService

DEFAULT_VIDEO_DURATION_S_FALLBACK = 5  # only used if a poll payload is somehow missing duration_s


class VideoGenerationPollingTask:
    """Polls one video generation request; a `WorkerTask`."""

    _MAX_VIDEO_POLL_ATTEMPTS = 24  # ~24 x 30s = 12 min budget, RFC §10 open question #3 starting point

    def __init__(
        self,
        *,
        video_registry: "ProviderRegistry",
        task_dispatch: "TaskDispatchService",
        ensure_agents: Callable[[str], Awaitable[Any]],
        notification_service: "UserNotificationService",
        media_storage: Any,
        quota_service: Optional[Any],
        link_service: Optional[Any],
        short_link_service: Optional[Any],
        deliver_video: Callable[..., Awaitable[None]],
    ) -> None:
        self._video_registry = video_registry
        self._task_dispatch = task_dispatch
        self._ensure_agents = ensure_agents
        self._notification = notification_service
        self._media_storage = media_storage
        self._quota_service = quota_service
        self._link_service = link_service
        self._short_link_service = short_link_service
        self._deliver_video = deliver_video

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        """
        Poll video generation status. Re-enqueue if pending. On done: deliver via
        deliver_video(). On failed/expired: notify the user directly, no silent
        drop. Kicked off by GrokVideoAdapter. See VIDEO_GENERATION_RFC.md §3.4 step 4.

        duration_s is resolved ONCE here (not re-read in the pending/done branches)
        so it survives multiple poll cycles: the re-enqueue call below threads it
        forward on every "pending" tick, otherwise TaskDispatchService's own
        duration_s=5 default would silently reset it on each re-poll and the
        deliver_video() billing call would under-bill a longer video.

        origin_platform is resolved the same way, for the same reason, but for a
        different bug: UserNotificationService.resolve_channel() only honors
        channel_id_override when platform_override is ALSO set (`if
        channel_id_override and platform_override:`) — omitting platform_override
        silently falls through to the user's primary/last-active channel instead
        of the channel the request actually came from. Live-verified 2026-08-24.
        """
        request_id      = payload.get("request_id", "")
        user_id         = payload.get("user_id", "")
        account_id      = payload.get("account_id", "")
        session_id      = payload.get("session_id", "")
        attempt         = payload.get("attempt", 0)
        provider        = payload.get("provider", "grok")
        duration_s      = payload.get("duration_s", DEFAULT_VIDEO_DURATION_S_FALLBACK)
        origin_platform = payload.get("origin_platform")

        origin_channel_id = session_id.split(":", 1)[1] if ":" in session_id else None

        video_port = None
        try:
            video_port = self._video_registry.get(provider)
        except ValueError:
            logger.debug("No video generation port registered for provider %r", provider)
        if not video_port:
            logger.error(
                f"[VideoGeneration] Missing dependencies in WorkerHandler "
                f"(provider={provider!r}, available="
                f"{self._video_registry.list_available()})"
            )
            return {"error": "video_generation not configured"}, 500

        # notify() routes through the eager Quick agent, absent on a cold instance until this runs.
        if user_id:
            await self._ensure_agents(user_id)

        if attempt >= self._MAX_VIDEO_POLL_ATTEMPTS:
            logger.warning(f"[VideoGeneration] Polling timeout: request={request_id[:16]}")
            await self._notification.notify(
                user_id=user_id, account_id=account_id,
                system_alert="Video generation timed out without producing a result.",
                kind=NotificationKind.DEEP_RESEARCH,
                channel_id_override=origin_channel_id,
                platform_override=origin_platform,
            )
            return {"status": "timeout"}, 200

        try:
            result: VideoPollResult = await video_port.get_status(request_id)
        except Exception as exc:
            logger.warning(
                f"[VideoGeneration] get_status failed, attempt={attempt}: {exc}"
            )
            await self._task_dispatch.enqueue_video_generation_polling(
                request_id=request_id, user_id=user_id, account_id=account_id,
                session_id=session_id, attempt=attempt + 1, delay_seconds=30,
                duration_s=duration_s, origin_platform=origin_platform,
            )
            return {"status": "retry", "attempt": attempt + 1}, 200

        if result.status == "pending":
            await self._task_dispatch.enqueue_video_generation_polling(
                request_id=request_id, user_id=user_id, account_id=account_id,
                session_id=session_id, attempt=attempt + 1, delay_seconds=30,
                duration_s=duration_s, origin_platform=origin_platform,
            )
            logger.info(f"[VideoGeneration] Pending, attempt={attempt + 1}")
            return {"status": "polling", "attempt": attempt + 1}, 200

        if result.status == "done":
            # Prefer the duration xAI actually reports on the finished render
            # (result.duration_s) over the poll payload's submission-time estimate —
            # the payload's value is a guess for create_video and simply wrong for
            # edit_video (which has no duration param at submission time at all).
            # Fall back to the payload only when the poll response didn't include one.
            effective_duration_s = (
                result.duration_s if result.duration_s is not None else duration_s
            )
            await self._deliver_video(
                video_data=result.data,
                user_id=user_id, account_id=account_id,
                duration_s=effective_duration_s,
                media_storage=self._media_storage,
                notification=self._notification,
                quota_service=self._quota_service,
                link_service=self._link_service,
                short_link_service=self._short_link_service,
                channel_id_override=origin_channel_id,
                platform_override=origin_platform,
            )
            logger.info(f"[VideoGeneration] Delivered to user={user_id[:8]}")
            return {"status": "delivered"}, 200

        # "failed" or "expired"
        logger.error(
            f"[VideoGeneration] {result.status}: request={request_id[:16]}, error={result.error}"
        )
        # result.error carries xAI's actual rejection reason (e.g. "Video is too
        # long. Maximum duration is 8.7 seconds.") — surface it verbatim instead of
        # a generic apology, so the user (and the formatter agent relaying this)
        # knows exactly what to fix, not just that something failed.
        alert = "Video generation did not complete — the AI provider returned an error."
        if result.error:
            alert += f" Reason: {result.error}"
        await self._notification.notify(
            user_id=user_id, account_id=account_id,
            system_alert=alert,
            kind=NotificationKind.DEEP_RESEARCH,
            channel_id_override=origin_channel_id,
            platform_override=origin_platform,
        )
        return {"status": result.status}, 200
