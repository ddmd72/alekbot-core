"""The OpenAI deep-research webhook only claims and enqueues; delivery runs in a named worker task (S1b task W)."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.web.deep_research_webhooks import create_deep_research_webhooks_blueprint

_META = {"user_id": "u1", "account_id": "a1", "query": "tides", "session_id": "u1:C9",
         "origin_platform": "telegram"}


def _app(task_queue, dedup=None, fetch=None):
    app = Quart("t")
    app.register_blueprint(create_deep_research_webhooks_blueprint(
        webhook_secret=None, task_queue=task_queue, dedup_store=dedup, fetch_response=fetch))
    return app


def _dedup(claimed=True):
    d = MagicMock()
    d.try_mark_processed = AsyncMock(return_value=claimed)
    d.release = AsyncMock()
    return d


async def _post(app, event_type="response.completed", data=None):
    body = {"type": event_type, "data": data if data is not None else {"id": "resp_1", "metadata": _META}}
    async with app.test_client() as c:
        return await c.post("/webhooks/openai/deep-research", data=json.dumps(body),
                            headers={"Content-Type": "application/json"})


async def test_claims_then_enqueues_named_task_and_returns_200():
    q, d = AsyncMock(), _dedup()
    resp = await _post(_app(q, d))
    assert resp.status_code == 200
    d.try_mark_processed.assert_awaited_once_with("openai_deep_research:resp_1")
    q.enqueue_worker_task.assert_awaited_once()
    kw = q.enqueue_worker_task.await_args.kwargs
    assert kw["task_type"] == "deep_research_webhook_event"
    assert kw["dedup_key"] == "resp_1"
    assert kw["payload"] == {
        "event_type": "response.completed", "job_id": "resp_1", "user_id": "u1", "account_id": "a1",
        "session_id": "u1:C9", "origin_platform": "telegram", "query": "tides"}
    d.release.assert_not_awaited()


async def test_no_report_text_travels_in_the_payload():
    q = AsyncMock()
    data = {"id": "resp_1", "metadata": _META, "output_text": "SECRET REPORT BODY"}
    await _post(_app(q, _dedup()), data=data)
    assert "SECRET REPORT BODY" not in json.dumps(q.enqueue_worker_task.await_args.kwargs["payload"])


async def test_duplicate_job_is_acked_without_enqueue():
    q, d = AsyncMock(), _dedup(claimed=False)
    resp = await _post(_app(q, d))
    assert resp.status_code == 200
    assert (await resp.get_json())["duplicate"] is True
    q.enqueue_worker_task.assert_not_awaited()


async def test_enqueue_failure_releases_the_claim_and_returns_500():
    q, d = AsyncMock(), _dedup()
    q.enqueue_worker_task.side_effect = RuntimeError("queue down")
    resp = await _post(_app(q, d))
    assert resp.status_code == 500
    d.release.assert_awaited_once_with("openai_deep_research:resp_1")


async def test_cancelled_request_during_enqueue_releases_the_claim():
    # Calls the view directly and cancels its task mid-enqueue: how the test client propagates a
    # CancelledError differs by platform (it hung forever on Linux CI), so it is kept out of the path.
    q, d = AsyncMock(), _dedup()
    entered, never = asyncio.Event(), asyncio.Event()

    async def _stuck_enqueue(**_kw):
        entered.set()
        await never.wait()

    q.enqueue_worker_task.side_effect = _stuck_enqueue
    app = _app(q, d)
    body = json.dumps({"type": "response.completed", "data": {"id": "resp_1", "metadata": _META}})
    view = app.view_functions["deep_research_webhooks.handle_openai_deep_research"]

    async with app.test_request_context("/webhooks/openai/deep-research", method="POST", data=body,
                                        headers={"Content-Type": "application/json"}):
        task = asyncio.ensure_future(view())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.CancelledError:
            pass
    assert task.cancelled()
    d.release.assert_awaited_once_with("openai_deep_research:resp_1")


async def test_missing_queue_releases_the_claim_and_returns_500():
    d = _dedup()
    resp = await _post(_app(None, d))
    assert resp.status_code == 500
    d.release.assert_awaited_once()


async def test_empty_user_is_acked_without_claim_or_enqueue():
    q, d = AsyncMock(), _dedup()
    resp = await _post(_app(q, d), data={"id": "resp_1", "metadata": {"x": 1}})
    assert resp.status_code == 200
    d.try_mark_processed.assert_not_awaited()
    q.enqueue_worker_task.assert_not_awaited()


async def test_unknown_event_type_is_ignored_without_retrieval():
    q, fetch = AsyncMock(), AsyncMock()
    resp = await _post(_app(q, _dedup(), fetch), event_type="batch.completed")
    assert resp.status_code == 200
    fetch.assert_not_awaited()
    q.enqueue_worker_task.assert_not_awaited()


async def test_retrieval_when_event_has_no_metadata():
    q, fetch = AsyncMock(), AsyncMock(return_value={"metadata": _META})
    resp = await _post(_app(q, _dedup(), fetch), data={"id": "resp_1"})
    assert resp.status_code == 200
    fetch.assert_awaited_once_with("resp_1")
    assert q.enqueue_worker_task.await_args.kwargs["payload"]["user_id"] == "u1"


async def test_retrieval_failure_is_500_without_claim():
    q, d, fetch = AsyncMock(), _dedup(), AsyncMock(side_effect=RuntimeError("api"))
    resp = await _post(_app(q, d, fetch), data={"id": "resp_1"})
    assert resp.status_code == 500
    d.try_mark_processed.assert_not_awaited()
