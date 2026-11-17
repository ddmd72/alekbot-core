"""The `deep_research_failed` `/worker` task: the research Cloud Run Job failed, tell the user in the origin channel.

The job cannot notify itself; it enqueues this task. `WorkerHandler` dispatches it here through its task registry.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Tuple

from ..domain import deep_research_notices as dr_notices

if TYPE_CHECKING:
    from .deep_research_notice_sender import DeepResearchNoticeSender


class DeepResearchFailedTask:
    """Sends the failure notice of one research job; a `WorkerTask`."""

    def __init__(self, *, ensure_agents: Callable[[str], Awaitable[Any]], notice_sender: DeepResearchNoticeSender) -> None:
        self._ensure_agents = ensure_agents
        self._notice_sender = notice_sender

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        """The research job (Cloud Run Job) cannot notify; it enqueues this task to tell the user."""
        user_id = payload.get("user_id")
        account_id = payload.get("account_id")
        if not user_id or not account_id:
            return {"error": "missing user_id or account_id"}, 400
        session_id = payload.get("session_id") or ""
        origin_channel_id = session_id.split(":", 1)[1] if ":" in session_id else None
        query = payload.get("query") or ""
        # notify() routes through the eager Quick agent, absent on a cold instance until this runs.
        await self._ensure_agents(user_id)
        alert = (
            dr_notices.report_not_prepared_notice(query)
            if payload.get("reason") == "delivery_failed"
            else dr_notices.research_failed_notice(query)
        )
        return await self._notice_sender.send(
            user_id, account_id, alert, origin_channel_id, payload.get("origin_platform") or None,
            context=f"reason={payload.get('reason')}",
        )
