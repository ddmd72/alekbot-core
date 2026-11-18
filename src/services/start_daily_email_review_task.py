"""The `start_daily_email_review` `/worker` task: the hourly fan-out that enqueues one `daily_email_review` per user."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Tuple

from ..ports.dedup_store import DedupStore
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .email_review_service import EmailReviewService
    from .task_dispatch_service import TaskDispatchService


class StartDailyEmailReviewTask:
    """Enqueues `daily_email_review` once per user per local date; a `WorkerTask`."""

    def __init__(
        self,
        *,
        email_review: "EmailReviewService",
        task_dispatch: "TaskDispatchService",
        user_repo: UserRepository,
        marks: Optional[DedupStore],
        deadline_seconds: int,
    ) -> None:
        self._email_review = email_review
        self._task_dispatch = task_dispatch
        self._user_repo = user_repo
        self._marks = marks
        self._deadline_seconds = deadline_seconds

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        """
        Fan-out: enqueue daily_email_review once per user per local date. Called hourly;
        a user is eligible during the first 3 hours from gmail_daily_review_hour and the
        (user, slot date) mark makes every later tick in the window a no-op.
        """
        now_utc = datetime.now(timezone.utc)
        eligible = await self._email_review.find_eligible_users(self._user_repo, now_utc)

        marks = self._marks
        if marks is None:
            logger.warning("[Worker] start_daily_email_review: no daily_review_marks store, duplicates possible")

        started = 0
        failed = 0
        for user in eligible:
            mark_key = f"daily_review:{user.user_id}:{user.slot_date.isoformat()}"
            if marks is not None:
                try:
                    claimed = await marks.try_mark_processed(mark_key)
                except Exception as exc:
                    # A double review is worse than a one-hour delay: retried next tick.
                    logger.error(
                        f"[Worker] start_daily_email_review: claim failed for {user.user_id[:8]}: {exc}",
                        exc_info=True,
                    )
                    failed += 1
                    continue
                if not claimed:
                    logger.info(
                        f"[Worker] start_daily_email_review: already started for "
                        f"{user.user_id[:8]} on {user.slot_date}"
                    )
                    continue
            try:
                await self._task_dispatch.enqueue_worker_task(
                    "daily_email_review",
                    {"user_id": user.user_id, "account_id": user.account_id, "slot_date": user.slot_date.isoformat()},
                    deadline_seconds=self._deadline_seconds,
                    # A queue call that errored after creating the task cannot yield a second task.
                    dedup_key=mark_key,
                )
            except Exception as exc:
                logger.error(
                    f"[Worker] start_daily_email_review: enqueue failed for {user.user_id[:8]}: {exc}",
                    exc_info=True,
                )
                failed += 1
                if marks is not None:
                    try:
                        await marks.release(mark_key)
                    except Exception as rel_exc:
                        logger.error(
                            f"[Worker] start_daily_email_review: mark release failed for "
                            f"{user.user_id[:8]}: {rel_exc}",
                            exc_info=True,
                        )
                continue
            started += 1
            logger.info(f"[Worker] start_daily_email_review: enqueued for {user.user_id[:8]}")

        logger.info(f"[Worker] start_daily_email_review complete: started={started} failed={failed}")
        return {"started": started, "failed": failed}, 200
