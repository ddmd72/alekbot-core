"""WorkerHandler task deep_research_webhook_event, the notice wording, and the named report task (S1b task W)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain import deep_research_notices as notices
from src.domain.notification_kind import NotificationKind
from src.handlers.worker_handler import WorkerHandler
from src.ports.task_claim_store import TaskClaimStore
from src.services.deep_research_delivery import deliver_deep_research

_PAYLOAD = {"task_type": "deep_research_webhook_event", "event_type": "response.completed",
            "job_id": "resp_1", "user_id": "u1", "account_id": "a1", "session_id": "u1:C9",
            "origin_platform": "telegram", "query": "tides"}


def _result(delivered=True, sent=True, error=None):
    return MagicMock(delivered=delivered, sent=sent, error=error)


def _worker(response=None, notify=None, fetch="default", store=None, claims=None):
    notification = AsyncMock()
    notification.notify.return_value = notify or _result()
    fetch_fn = AsyncMock(return_value=response or {"output_text": "REPORT", "model": "m"}) if fetch == "default" else fetch
    worker = WorkerHandler(
        agent_worker_handler=AsyncMock(), email_indexing_service=MagicMock(),
        notification_service=notification, consolidation_service=None,
        coordinator=MagicMock(), agent_factory=AsyncMock(),
        indexed_email_repo=None, user_repo=MagicMock(), task_dispatch=AsyncMock(),
        media_storage=MagicMock(), deep_research_fetch=fetch_fn, prompt_content_store=store,
        task_claims=claims,
    )
    return worker, notification, fetch_fn


def _payload(**kw):
    return {**_PAYLOAD, **kw}


class TestCompleted:
    async def test_delivers_once_to_origin_channel_and_platform(self):
        store = AsyncMock()
        worker, _, fetch = _worker(store=store)
        with patch("src.handlers.worker_handler.deliver_deep_research", new=AsyncMock(return_value=True)) as dl:
            body, status = await worker._dispatch(_payload(), {})
        assert status == 200
        fetch.assert_awaited_once_with("resp_1")
        dl.assert_awaited_once()
        kw = dl.await_args.kwargs
        assert kw["result_text"] == "REPORT" and kw["query"] == "tides"
        assert kw["channel_id_override"] == "C9" and kw["platform_override"] == "telegram"
        assert kw["report_dedup_id"] == "resp_1"
        store.record_dr_result.assert_awaited_once()

    async def test_result_is_captured_before_delivery(self):
        order = []
        store = AsyncMock()
        store.record_dr_result.side_effect = lambda **_: order.append("store")
        worker, _, _ = _worker(store=store)
        async def _dl(**_):
            order.append("deliver")
            return True
        with patch("src.handlers.worker_handler.deliver_deep_research", new=_dl):
            await worker._dispatch(_payload(), {})
        assert order == ["store", "deliver"]

    async def test_report_task_not_enqueued_is_200_not_a_retry(self):
        worker, _, _ = _worker()
        with patch("src.handlers.worker_handler.deliver_deep_research", new=AsyncMock(return_value=False)):
            _, status = await worker._dispatch(_payload(), {})
        assert status == 200

    async def test_html_report_task_is_named_by_job_id(self):
        worker, _, _ = _worker()
        await worker._dispatch(_payload(), {})
        worker._task_dispatch.enqueue_agent_task.assert_awaited_once()
        assert worker._task_dispatch.enqueue_agent_task.await_args.kwargs["dedup_key"] == "deep_research_report:resp_1"


class TestNotices:
    @pytest.mark.parametrize("event,response,word", [
        ("response.failed", {"error": {"message": "x"}}, "error"),
        ("response.incomplete", {"incomplete_details": {"reason": "max"}}, "incomplete"),
        ("response.cancelled", {}, "cancelled"),
    ])
    async def test_one_notice_with_query_origin_and_no_retry_wording(self, event, response, word):
        worker, notification, _ = _worker(response=response)
        _, status = await worker._dispatch(_payload(event_type=event), {})
        assert status == 200
        notification.notify.assert_awaited_once()
        kw = notification.notify.await_args.kwargs
        assert kw["kind"] == NotificationKind.DEEP_RESEARCH
        assert kw["channel_id_override"] == "C9" and kw["platform_override"] == "telegram"
        assert kw["suppress_transient_retry"] is True
        assert "tides" in kw["system_alert"] and "ask for it again" in kw["system_alert"]
        assert word in kw["system_alert"]
        assert "retried" not in kw["system_alert"]

    async def test_not_delivered_and_not_sent_is_500(self):
        worker, _, _ = _worker(notify=_result(delivered=False, sent=False, error="x"))
        _, status = await worker._dispatch(_payload(event_type="response.cancelled"), {})
        assert status == 500

    async def test_sent_but_not_delivered_is_200(self):
        worker, _, _ = _worker(notify=_result(delivered=False, sent=True))
        _, status = await worker._dispatch(_payload(event_type="response.failed"), {})
        assert status == 200


class TestGuards:
    @pytest.mark.parametrize("drop", ["event_type", "job_id", "user_id", "account_id"])
    async def test_missing_field_is_400(self, drop):
        worker, _, fetch = _worker()
        _, status = await worker._dispatch({k: v for k, v in _PAYLOAD.items() if k != drop}, {})
        assert status == 400
        fetch.assert_not_awaited()

    async def test_no_fetch_is_422(self):
        worker, _, _ = _worker(fetch=None)
        _, status = await worker._dispatch(_payload(), {})
        assert status == 422

    async def test_retrieval_failure_is_500(self):
        worker, notification, _ = _worker(fetch=AsyncMock(side_effect=RuntimeError("api")))
        _, status = await worker._dispatch(_payload(), {})
        assert status == 500
        notification.notify.assert_not_awaited()

    async def test_unknown_event_type_is_400(self):
        worker, notification, _ = _worker()
        _, status = await worker._dispatch(_payload(event_type="response.weird"), {})
        assert status == 400
        notification.notify.assert_not_awaited()


class TestJobFailedNotice:
    _JOB = {"task_type": "deep_research_failed", "user_id": "u1", "account_id": "a1", "session_id": "u1:C9",
            "origin_platform": "telegram", "query": "tides", "reason": "agent_failed"}

    async def test_sent_but_not_delivered_is_200_and_suppresses_agent_retry(self):
        worker, notification, _ = _worker(notify=_result(delivered=False, sent=True))
        _, status = await worker._dispatch(dict(self._JOB), {})
        assert status == 200
        assert notification.notify.await_args.kwargs["suppress_transient_retry"] is True

    async def test_not_sent_is_500(self):
        worker, _, _ = _worker(notify=_result(delivered=False, sent=False, error="x"))
        _, status = await worker._dispatch(dict(self._JOB), {})
        assert status == 500

    async def test_delivery_failed_reason_uses_report_not_prepared_wording(self):
        worker, notification, _ = _worker()
        await worker._dispatch({**self._JOB, "reason": "delivery_failed"}, {})
        assert notification.notify.await_args.kwargs["system_alert"] == notices.report_not_prepared_notice("tides")


class TestWording:
    def test_every_notice_names_the_query_and_invites_asking_again(self):
        for fn in (notices.research_failed_notice, notices.provider_error_notice, notices.incomplete_notice,
                   notices.cancelled_notice, notices.report_not_prepared_notice):
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


def _claims():
    claims = AsyncMock(spec=TaskClaimStore)
    claims.try_claim.return_value = True
    return claims


class TestThroughHandle:
    async def test_500_releases_the_task_claim(self):
        from src.domain.cloud_task_delivery import CloudTaskDelivery
        claims = _claims()
        worker, _, _ = _worker(fetch=AsyncMock(side_effect=RuntimeError("api")), claims=claims)
        delivery = MagicMock(spec=CloudTaskDelivery)
        delivery.task_name = "projects/p/tasks/deep_research_webhook_event-abc"
        _, status = await worker.handle(dict(_PAYLOAD), delivery)
        assert status == 500
        key, attempt = claims.try_claim.await_args.args
        assert key == delivery.task_name
        claims.release.assert_awaited_once_with(delivery.task_name, attempt)
        claims.mark_done.assert_not_awaited()

    async def test_200_keeps_the_task_claim(self):
        from src.domain.cloud_task_delivery import CloudTaskDelivery
        claims = _claims()
        worker, _, _ = _worker(response={}, claims=claims)
        delivery = MagicMock(spec=CloudTaskDelivery)
        delivery.task_name = "t"
        _, status = await worker.handle(_payload(event_type="response.cancelled"), delivery)
        assert status == 200
        key, attempt = claims.try_claim.await_args.args
        assert key == "t"
        claims.mark_done.assert_awaited_once_with("t", attempt)
        claims.release.assert_not_awaited()


def test_main_py_wires_fetch_and_store_into_worker_handler():
    # The WorkerHandler the main service serves is built by build_application_graph (P9 T2).
    import ast
    from pathlib import Path
    source = Path(__file__).resolve().parents[3] / "src" / "composition" / "application_graph.py"
    tree = ast.parse(source.read_text())
    kwargs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "WorkerHandler":
            kwargs = [kw.arg for kw in node.keywords]
    assert "deep_research_fetch" in kwargs and "prompt_content_store" in kwargs
