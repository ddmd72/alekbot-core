"""
Task Queue Port
===============

Defines the interface for async task queues used by adapters (e.g., Cloud Tasks).
"""
from typing import Protocol, Dict, Any, Optional


class TaskQueue(Protocol):
    """Port for enqueueing background tasks.

    Cloud Tasks semantics: at-least-once delivery, no order between tasks. A named create meets ALREADY_EXISTS while a
    task of that name is queued or running and for about an hour after it ran or was deleted; unnamed creates are never
    deduplicated. Payloads are JSON: Pydantic models as their dump, datetimes/dates as ISO-8601, enums as their value;
    any other non-JSON value raises TypeError before a create. A create that fails raises; whether it landed is unknown
    to the caller (a lost answer), which is what names are for.
    """

    async def enqueue_slack_event(
        self,
        event_data: Dict[str, Any],
        session_id: str,
        delay_seconds: int = 0,
        trace_headers: Optional[Dict[str, str]] = None,
        busy_requeues: int = 0,
        task_name: Optional[str] = None,
    ) -> str:
        """Enqueue a Slack event for background processing. busy_requeues counts re-queues of a busy session.

        task_name: deterministic Cloud Tasks name; ALREADY_EXISTS (task already queued) counts as success.

        POST {service_url}/worker, dispatch deadline 1800 s, schedule time now+delay when delay_seconds > 0. With
        task_name, ALREADY_EXISTS is success and makes no second task. Returns a name for logging (no caller reads it)."""
        ...

    async def create_queue_if_not_exists(self) -> None:
        """Create the queue when get_queue answers NotFound. Synchronous gRPC on the loop: startup only. Errors raise."""
        ...

    def get_queue_stats(self) -> Dict[str, Any]:
        """Return queue stats. No caller in src/."""
        ...

    async def purge_queue(self) -> None:
        """Purge all queued tasks. No caller in src/."""
        ...

    async def enqueue_agent_task(
        self,
        agent_id: str,
        intent: str,
        query: str,
        context: Dict[str, Any],
        deadline_seconds: Optional[int] = None,
        dedup_key: Optional[str] = None,
    ) -> str:
        """
        Enqueue an async agent task for background execution.

        Returns task name (Cloud Tasks task ID).
        Worker receives payload with task_type="agent_execution".
        deadline_seconds: Cloud Tasks dispatch_deadline override (default = Cloud Tasks default ~10 min).
                          Set to 1800 for long-running research tasks (Claude deep research).
        dedup_key: names the task, so a second enqueue with the same key (a retried create whose
                   first attempt succeeded) returns the existing task's name instead of a second task.
                   The name is `agent_execution-<sha256(key)[:32]>`; ALREADY_EXISTS returns that full name.
                   Without a key every call makes a task. The dispatch deadline is set only when given.
        """
        ...

    async def enqueue_consolidation_task(self, user_id: str) -> str:
        """
        Enqueue a consolidation task for a user.

        Returns task name (Cloud Tasks task ID).
        Worker receives payload with task_type="consolidation".
        Unnamed (a repeat makes a second task); dispatch deadline 1800 s.
        """
        ...

    async def enqueue_companion_consolidation_task(self, session_id: str) -> str:
        """
        Enqueue a companion extraction task for a session.

        Returns task name (Cloud Tasks task ID).
        Worker receives payload with task_type="companion_consolidation".
        Unnamed; dispatch deadline 600 s.
        """
        ...

    async def enqueue_video_generation_polling(
        self,
        request_id: str,
        user_id: str,
        account_id: str,
        session_id: str = "",
        duration_s: int = 5,
        origin_platform: Optional[str] = None,
        attempt: int = 0,
        delay_seconds: int = 30,
    ) -> str:
        """
        Enqueue a video_generation_polling Cloud Task.

        Worker receives payload with task_type="video_generation_polling".
        duration_s: video duration in seconds, carried in the payload for billing accuracy
                    in WorkerHandler's eventual billing call.
        origin_platform: platform the request came from ("slack"/"telegram"), carried in the
                    payload so delivery reaches that platform's channel — UserNotificationService's
                    channel_id_override is silently ignored unless platform_override is ALSO set.
        attempt: retry counter for timeout guard.
        delay_seconds: schedule this many seconds in the future (Cloud Tasks schedule_time).
        First enqueue uses delay_seconds=30 (no point polling immediately after kick-off).
        session_id: original conversation session for delivery back to user's thread.
        Unnamed; no dispatch deadline.
        """
        ...


    async def enqueue_email_indexing_task(self, job_id: str) -> str:
        """
        Enqueue one email indexing page for a running job.

        Returns task name (Cloud Tasks task ID).
        Worker receives payload with task_type="email_indexing" + job_id.
        One Cloud Tasks request = one Gmail page (~300 emails) = full CPU allocation.
        Worker re-enqueues if job.next_page_token is set after processing.
        Unnamed; no dispatch deadline.
        """
        ...

    async def enqueue_worker_task(
        self,
        task_type: str,
        payload: Dict[str, Any],
        delay_seconds: int = 0,
        deadline_seconds: Optional[int] = None,
        dedup_key: Optional[str] = None,
    ) -> Optional[str]:
        """
        Enqueue a generic worker task by task_type.

        Used for custom task types (e.g. setup_microsoft_todo, reindex_task_list,
        renew_task_subscriptions) that don't warrant a dedicated typed method.
        Worker receives payload with task_type injected automatically.
        delay_seconds: schedule this many seconds in the future.
        deadline_seconds: Cloud Tasks dispatch_deadline override. Omitting it takes
            the Cloud Tasks default of 600s, which SILENTLY caps any longer in-process
            budget — task types whose NotificationSLA exceeds 10 minutes
            (execute_reminder at PERFORMANCE, daily_email_review) must pass this or
            their budget is fiction. Maximum accepted by Cloud Tasks is 1800s, and it
            must also stay within the Cloud Run request timeout.
        dedup_key: when provided, de-duplicates this task against any other task of
            the same task_type carrying the same key — Cloud Tasks (or whatever backend
            implements this port) rejects a second enqueue while the first is
            queued/running/recently completed. Callers never construct a task name
            themselves. Returns None when the enqueue was a no-op due to such a duplicate.
        Returns task name (Cloud Tasks task ID), or None on a de-duplicated no-op.
        The name is `{task_type}-<sha256(dedup_key)[:32]>`. The body is {"task_type": task_type, **payload}.
        """
        ...