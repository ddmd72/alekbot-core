"""WorkerHandler task deep_research_failed, the notice wording, and the named report task (S1b task W)."""
from unittest.mock import AsyncMock, MagicMock

from src.composition.worker_setup import build_worker_tasks
from src.domain import deep_research_notices as notices
from src.handlers.worker_handler import WorkerHandler
from src.services.deep_research_delivery import deliver_deep_research


def _result(delivered=True, sent=True, error=None):
    return MagicMock(delivered=delivered, sent=sent, error=error)


def _worker(notify=None, claims=None):
    notification = AsyncMock()
    notification.notify.return_value = notify or _result()
    agent_factory = AsyncMock()
    task_dispatch = AsyncMock()
    worker = WorkerHandler(
        agent_worker_handler=AsyncMock(), email_indexing_service=MagicMock(),
        consolidation_service=None,
        agent_factory=agent_factory,
        user_repo=MagicMock(), task_dispatch=task_dispatch,
        task_claims=claims,
        tasks=build_worker_tasks(
            email_review=None, daily_review_marks=None, gmail_reconnect_flag=None, account_repo=None,
            notes_port=None, user_repo=MagicMock(), ensure_agents=agent_factory.ensure_agents_for_user,
            notification_service=notification, alert_sink=None, task_dispatch=task_dispatch,
            video_registry=None, quota_service=None, media_storage=MagicMock(), link_service=None,
            short_link_service=None),
    )
    return worker, notification


class TestJobFailedNotice:
    _JOB = {"task_type": "deep_research_failed", "user_id": "u1", "account_id": "a1", "session_id": "u1:C9",
            "origin_platform": "telegram", "query": "tides", "reason": "agent_failed"}

    async def test_sent_but_not_delivered_is_200_and_suppresses_agent_retry(self):
        worker, notification = _worker(notify=_result(delivered=False, sent=True))
        _, status = await worker._dispatch(dict(self._JOB), {})
        assert status == 200
        assert notification.notify.await_args.kwargs["suppress_transient_retry"] is True

    async def test_not_sent_is_500(self):
        worker, _ = _worker(notify=_result(delivered=False, sent=False, error="x"))
        _, status = await worker._dispatch(dict(self._JOB), {})
        assert status == 500

    async def test_delivery_failed_reason_uses_report_not_prepared_wording(self):
        worker, notification = _worker()
        await worker._dispatch({**self._JOB, "reason": "delivery_failed"}, {})
        assert notification.notify.await_args.kwargs["system_alert"] == notices.report_not_prepared_notice("tides")


class TestWording:
    def test_every_notice_names_the_query_and_invites_asking_again(self):
        for fn in (notices.research_failed_notice, notices.report_not_prepared_notice):
            text = fn("tides")
            assert '"tides"' in text and "ask for it again" in text

    def test_empty_query_falls_back(self):
        assert "the request" in notices.research_failed_notice("")


class TestDeliverReportTask:
    async def _run(self, **kw):
        queue = AsyncMock()
        notifier = AsyncMock()
        ok = await deliver_deep_research(result_text="t", user_id="u1", account_id="a1", query="tides",
                                         task_queue=queue, failure_notification=notifier, **kw)
        return ok, queue, notifier

    async def test_task_is_named_when_an_id_is_given(self):
        _, queue, _ = await self._run(report_dedup_id="job9")
        assert queue.enqueue_agent_task.await_args.kwargs["dedup_key"] == "deep_research_report:job9"

    async def test_task_is_unnamed_without_an_id(self):
        _, queue, _ = await self._run()
        assert "dedup_key" not in queue.enqueue_agent_task.await_args.kwargs

    async def test_enqueue_failure_notice_has_the_shared_wording(self):
        queue = AsyncMock()
        queue.enqueue_agent_task.side_effect = RuntimeError("down")
        notifier = AsyncMock()
        ok = await deliver_deep_research(result_text="t", user_id="u1", account_id="a1", query="tides",
                                         task_queue=queue, failure_notification=notifier)
        assert ok is False
        assert notifier.notify.await_args.kwargs["system_alert"] == notices.report_not_prepared_notice("tides")
