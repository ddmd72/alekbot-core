"""Deep research reports return to the platform+channel the request came from (S1b task E).

Covers the agent -> port hand-off, both live adapters (Claude job context, OpenAI metadata),
job_main, the OpenAI webhook and the worker task it enqueues; context/metadata without the field behave as before.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from quart import Quart

import job_main
from src.adapters.claude_deep_research_adapter import ClaudeDeepResearchAdapter
from src.adapters.openai_deep_research_adapter import OpenAIDeepResearchAdapter
from src.agents.deep_research_agent import DeepResearchAgent
from src.domain.cloud_task_delivery import CloudTaskDelivery
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.user import PerformanceTier
from src.ports.deep_research_port import DeepResearchPort
from src.ports.job_runner_port import JobRunnerPort
from src.handlers.worker_handler import WorkerHandler
from src.web.deep_research_webhooks import create_deep_research_webhooks_blueprint


# --- agent -> port ---------------------------------------------------------

def _agent_and_port():
    port = AsyncMock(spec=DeepResearchPort)
    port.create_interaction.return_value = "job-1"
    agent = DeepResearchAgent(
        config=AgentConfig(agent_id="dr", agent_type="deep_research", timeout_ms=60_000,
                           capabilities=["deep_research"]),
        job_port=port, tier=PerformanceTier.BALANCED, prompt_builder=None, user_id="u1",
    )
    return agent, port


def _msg(context):
    return AgentMessage(
        task_id="t", sender="smart", recipient="dr", intent=AgentIntent.QUERY,
        payload={"query": "q", "brief": "q", "language": "English"}, context=context,
    )


class TestAgentPassesOriginPlatform:
    async def test_origin_platform_from_context_reaches_port(self):
        agent, port = _agent_and_port()
        await agent.execute(_msg({"user_id": "u1", "account_id": "a1",
                                  "session_id": "u1:C1", "origin_platform": "telegram"}))
        assert port.create_interaction.await_args.kwargs["origin_platform"] == "telegram"

    async def test_missing_origin_platform_is_none(self):
        agent, port = _agent_and_port()
        await agent.execute(_msg({"user_id": "u1", "account_id": "a1"}))
        assert port.create_interaction.await_args.kwargs["origin_platform"] is None


# --- Claude adapter --------------------------------------------------------

class TestClaudeAdapterContext:
    async def _ctx(self, **kw):
        runner = AsyncMock(spec=JobRunnerPort)
        adapter = ClaudeDeepResearchAdapter(job_runner=runner, job_name="j")
        await adapter.create_interaction(
            query="q", user_id="u", account_id="a", original_query="o", session_id="u:C1", **kw)
        return json.loads(runner.run_job.call_args.kwargs["env_overrides"]["JOB_CONTEXT_JSON"])

    async def test_origin_platform_in_job_context(self):
        assert (await self._ctx(origin_platform="slack"))["origin_platform"] == "slack"

    async def test_absent_platform_is_empty_string(self):
        assert (await self._ctx())["origin_platform"] == ""


# --- job_main --------------------------------------------------------------

async def _run_job_with(monkeypatch, context):
    monkeypatch.setenv("JOB_QUERY", "q")
    monkeypatch.setenv("JOB_CONTEXT_JSON", json.dumps(context))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("GCS_MEDIA_BUCKET", raising=False)
    agent = MagicMock()
    agent.agent_id = "x"
    agent.execute = AsyncMock(return_value=AgentResponse(
        task_id="t", agent_id="x", status=AgentStatus.SUCCESS, confidence=1.0, result={"text": "report"}))
    with patch.object(job_main, "ClaudeDeepResearchRunnerAgent", return_value=agent), \
         patch.object(job_main, "_build_task_queue", return_value=MagicMock()), \
         patch.object(job_main.anthropic, "AsyncAnthropic"), \
         patch.object(job_main, "_capture_research_result", AsyncMock()), \
         patch.object(job_main, "_record_billing", AsyncMock()), \
         patch.object(job_main, "deliver_deep_research", AsyncMock()) as deliver:
        await job_main._run_job()
    return deliver.await_args.kwargs


class TestJobMainOverrides:
    async def test_passes_channel_and_platform(self, monkeypatch):
        kw = await _run_job_with(monkeypatch, {
            "user_id": "u", "account_id": "a", "session_id": "u:C1", "origin_platform": "telegram"})
        assert kw["channel_id_override"] == "C1"
        assert kw["platform_override"] == "telegram"

    async def test_old_context_has_no_override_and_does_not_crash(self, monkeypatch):
        kw = await _run_job_with(monkeypatch, {"user_id": "u", "account_id": "a"})
        assert kw["channel_id_override"] is None
        assert kw["platform_override"] is None


# --- OpenAI adapter + webhook ---------------------------------------------

class TestOpenAIMetadata:
    async def _metadata(self, **kw):
        adapter = OpenAIDeepResearchAdapter(api_key="k")
        adapter._client = AsyncMock()
        adapter._client.responses.create.return_value = MagicMock(id="resp-123456789012345678")
        await adapter.create_interaction(
            query="q", user_id="u", account_id="a", original_query="o", session_id="u:C1", **kw)
        return adapter._client.responses.create.call_args.kwargs["metadata"]

    async def test_metadata_carries_platform(self):
        md = await self._metadata(origin_platform="telegram")
        assert md["origin_platform"] == "telegram"
        assert len(md) <= 16 and all(isinstance(v, str) and len(v) <= 512 for v in md.values())

    async def test_metadata_without_platform_is_empty_string(self):
        assert (await self._metadata())["origin_platform"] == ""



# Since S1b task W the webhook only verifies, filters, retrieves, claims and enqueues the named
# worker task `deep_research_webhook_event`; WorkerHandler delivers the report or the notice.

def _webhook_app(task_queue=None, fetch=None, dedup=None):
    app = Quart("t")
    app.register_blueprint(create_deep_research_webhooks_blueprint(
        webhook_secret=None, task_queue=task_queue, fetch_response=fetch, dedup_store=dedup))
    return app


async def _post_webhook(app, event_type, data):
    async with app.test_client() as c:
        return await c.post("/webhooks/openai/deep-research",
                            data=json.dumps({"type": event_type, "data": data}),
                            headers={"Content-Type": "application/json"})


def _dr_worker(response=None, notification=None, task_dedup=None):
    notification = notification or AsyncMock()
    if notification.notify.side_effect is None:
        notification.notify.return_value = MagicMock(delivered=True, sent=True, error=None)
    worker = WorkerHandler(
        agent_worker_handler=AsyncMock(), email_indexing_service=MagicMock(),
        notification_service=notification, consolidation_service=None,
        coordinator=MagicMock(), agent_factory=AsyncMock(),
        indexed_email_repo=None, user_repo=MagicMock(), task_dispatch=AsyncMock(),
        media_storage=MagicMock(), task_dedup=task_dedup,
        deep_research_fetch=AsyncMock(return_value=response or {"output_text": "text", "model": "m"}),
    )
    return worker, notification


def _dr_event(event_type="response.completed", **kw):
    return {"task_type": "deep_research_webhook_event", "event_type": event_type, "job_id": "r1",
            "user_id": "u", "account_id": "a", "query": "q", "session_id": "u:C1",
            "origin_platform": "telegram", **kw}


class TestWebhookPlatform:
    async def test_platform_override_from_metadata(self):
        """Webhook: metadata's platform travels in the task payload; worker: it becomes the override."""
        queue = AsyncMock()
        resp = await _post_webhook(_webhook_app(queue), "response.completed", {
            "id": "resp_1", "output_text": "text", "metadata": {
                "user_id": "u", "account_id": "a", "query": "q",
                "session_id": "u:C1", "origin_platform": "telegram"}})
        assert resp.status_code == 200
        payload = queue.enqueue_worker_task.await_args.kwargs["payload"]
        assert payload["origin_platform"] == "telegram" and payload["session_id"] == "u:C1"

        worker, _ = _dr_worker()
        with patch("src.handlers.worker_handler.deliver_deep_research",
                   new_callable=AsyncMock, return_value=True) as deliver:
            _, status = await worker._dispatch({"task_type": "deep_research_webhook_event", **payload}, {})
        assert status == 200
        kw = deliver.call_args.kwargs
        assert kw["platform_override"] == "telegram"
        assert kw["channel_id_override"] == "C1"

    @pytest.mark.parametrize("md_extra", [{}, {"origin_platform": ""}])
    async def test_missing_platform_is_no_override(self, md_extra):
        queue = AsyncMock()
        resp = await _post_webhook(_webhook_app(queue), "response.completed", {
            "id": "resp_1", "output_text": "text", "metadata": {
                "user_id": "u", "account_id": "a", "query": "q", "session_id": "u:C1", **md_extra}})
        assert resp.status_code == 200
        payload = queue.enqueue_worker_task.await_args.kwargs["payload"]

        worker, _ = _dr_worker()
        with patch("src.handlers.worker_handler.deliver_deep_research",
                   new_callable=AsyncMock, return_value=True) as deliver:
            _, status = await worker._dispatch({"task_type": "deep_research_webhook_event", **payload}, {})
        assert status == 200
        assert deliver.call_args.kwargs["platform_override"] is None

    async def test_absent_platform_in_an_old_payload_is_no_override(self):
        worker, _ = _dr_worker()
        event = {k: v for k, v in _dr_event().items() if k != "origin_platform"}
        with patch("src.handlers.worker_handler.deliver_deep_research",
                   new_callable=AsyncMock, return_value=True) as deliver:
            await worker._dispatch(event, {})
        assert deliver.call_args.kwargs["platform_override"] is None


# --- failure events and event-carries-only-id --------------------------------
# test_failure_and_cancel_notify_origin_channel: covered by test_worker_deep_research_webhook_event.py
#   TestNotices.test_one_notice_with_query_origin_and_no_retry_wording (failed/incomplete/cancelled →
#   one notice, channel_id_override + platform_override) and tests/unit/web/test_deep_research_webhooks.py
#   TestOpenAIWebhookFailureEvents.test_failure_event_is_enqueued_for_the_worker.
# test_event_with_only_id_retrieves_response: covered by test_deep_research_webhook_enqueue.py
#   test_retrieval_when_event_has_no_metadata (fetch by id, user from the retrieved metadata) and
#   TestCompleted.test_delivers_once_to_origin_channel_and_platform (worker re-retrieves by job id;
#   result_text, channel, platform).

class TestWebhookFailureAndRetrieval:
    _MD = {"user_id": "u", "account_id": "a", "query": "q",
           "session_id": "u:C1", "origin_platform": "telegram"}

    async def test_failed_event_with_only_id_notifies_origin_channel(self):
        fetch = AsyncMock(return_value={"metadata": self._MD})
        queue = AsyncMock()
        resp = await _post_webhook(_webhook_app(queue, fetch=fetch), "response.failed", {"id": "r1"})
        assert resp.status_code == 200
        fetch.assert_awaited_once_with("r1")
        payload = queue.enqueue_worker_task.await_args.kwargs["payload"]
        assert payload["event_type"] == "response.failed" and payload["user_id"] == "u"

        worker, notification = _dr_worker(response={"metadata": self._MD})
        _, status = await worker._dispatch({"task_type": "deep_research_webhook_event", **payload}, {})
        assert status == 200
        assert notification.notify.await_args.kwargs["user_id"] == "u"
        assert notification.notify.await_args.kwargs["channel_id_override"] == "C1"

    async def test_retrieval_failure_is_non_2xx_so_openai_redelivers(self):
        fetch = AsyncMock(side_effect=RuntimeError("boom"))
        queue = AsyncMock()
        resp = await _post_webhook(_webhook_app(queue, fetch=fetch), "response.completed", {"id": "r1"})
        assert resp.status_code == 500
        queue.enqueue_worker_task.assert_not_called()

class TestOpenAIFetchResponse:
    async def test_returns_metadata_and_output(self):
        adapter = OpenAIDeepResearchAdapter(api_key="k")
        adapter._client = AsyncMock()
        adapter._client.responses.retrieve.return_value = MagicMock(
            metadata={"user_id": "u"}, output_text="out", model="m",
            error=None, incomplete_details=None)
        got = await adapter.fetch_response("r1")
        assert got == {"metadata": {"user_id": "u"}, "output_text": "out", "model": "m",
                       "error": None, "incomplete_details": None}

    async def test_error_is_returned_as_dict(self):
        adapter = OpenAIDeepResearchAdapter(api_key="k")
        adapter._client = AsyncMock()
        err = MagicMock()
        err.model_dump.return_value = {"code": "server_error", "message": "x"}
        adapter._client.responses.retrieve.return_value = MagicMock(
            metadata={}, output_text="", model="m", error=err, incomplete_details=None)
        assert (await adapter.fetch_response("r1"))["error"] == {"code": "server_error", "message": "x"}


# --- deliver_deep_research follow-on task context -------------------------

class TestDeliverCarriesOrigin:
    async def _context(self, **kw):
        from src.services.deep_research_delivery import deliver_deep_research
        queue = AsyncMock()
        await deliver_deep_research(result_text="r", user_id="u", account_id="a", query="q",
                                    task_queue=queue, session_id="u:C1", **kw)
        return queue.enqueue_agent_task.call_args.kwargs["context"]

    async def test_origin_channel_and_platform_in_html_task_context(self):
        ctx = await self._context(channel_id_override="C1", platform_override="telegram")
        assert ctx["origin_channel_id"] == "C1"
        assert ctx["origin_platform"] == "telegram"

    async def test_no_overrides_leaves_keys_out(self):
        ctx = await self._context()
        assert "origin_channel_id" not in ctx and "origin_platform" not in ctx


# --- round 2: event filter, dedup, empty user, incomplete -----------------

class _FakeDedup:
    def __init__(self):
        self.claimed, self.released = set(), []

    async def try_mark_processed(self, key):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    async def release(self, key):
        self.claimed.discard(key)
        self.released.append(key)




_MD2 = {"user_id": "u", "account_id": "a", "query": "q",
        "session_id": "u:C1", "origin_platform": "telegram"}


class TestEventFilter:
    @pytest.mark.parametrize("event", ["batch.completed", "fine_tuning.job.succeeded", "realtime.call.incoming"])
    async def test_other_families_are_acked_without_retrieval(self, event):
        fetch, queue, dedup = AsyncMock(), AsyncMock(), _FakeDedup()
        resp = await _post_webhook(_webhook_app(queue, fetch=fetch, dedup=dedup), event, {"id": "x1"})
        assert resp.status_code == 200
        fetch.assert_not_called()
        queue.enqueue_worker_task.assert_not_called()
        assert not dedup.claimed


class TestDedup:
    async def test_claim_is_recorded_before_enqueue(self):
        dedup = _FakeDedup()
        log = []
        orig = dedup.try_mark_processed

        async def claim(key):
            log.append("claim")
            return await orig(key)

        async def enqueue(**_):
            log.append("enqueue")

        dedup.try_mark_processed = claim
        queue = AsyncMock()
        queue.enqueue_worker_task.side_effect = enqueue
        resp = await _post_webhook(_webhook_app(queue, dedup=dedup), "response.completed",
                                   {"id": "r1", "metadata": _MD2, "output_text": "t"})
        assert resp.status_code == 200
        assert log == ["claim", "enqueue"]
        assert "openai_deep_research:r1" in dedup.claimed

    # test_duplicate_is_skipped_with_200: covered by test_deep_research_webhook_enqueue.py
    #   test_duplicate_job_is_acked_without_enqueue (200, duplicate=True, nothing enqueued).

    async def test_delivery_raise_releases_claim_and_fails_the_attempt(self):
        """The delivery now runs in the worker task: a raise releases the task claim (Cloud Tasks
        retries the attempt); the webhook's job mark stays, the task already holds the work."""
        dedup = _FakeDedup()
        worker, _ = _dr_worker(task_dedup=dedup)
        delivery = MagicMock(spec=CloudTaskDelivery)
        delivery.task_name = "projects/p/tasks/deep_research_webhook_event-r1"
        with patch("src.handlers.worker_handler.deliver_deep_research",
                   new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                await worker.handle(_dr_event(), delivery)
        assert dedup.released == [delivery.task_name]
        assert delivery.task_name not in dedup.claimed

    async def test_notify_raise_releases_claim_and_fails_the_attempt(self):
        dedup = _FakeDedup()
        notification = AsyncMock()
        notification.notify.side_effect = RuntimeError("boom")
        worker, _ = _dr_worker(notification=notification, task_dedup=dedup)
        delivery = MagicMock(spec=CloudTaskDelivery)
        delivery.task_name = "projects/p/tasks/deep_research_webhook_event-r2"
        with pytest.raises(RuntimeError):
            await worker.handle(_dr_event("response.failed", job_id="r2"), delivery)
        assert dedup.released == [delivery.task_name]


class TestEmptyUserAndIncomplete:
    async def test_empty_user_acked_without_delivery(self):
        fetch = AsyncMock(return_value={"metadata": {}, "output_text": "t"})
        dedup, queue = _FakeDedup(), AsyncMock()
        resp = await _post_webhook(_webhook_app(queue, fetch=fetch, dedup=dedup), "response.completed",
                                   {"id": "r3"})
        assert resp.status_code == 200
        queue.enqueue_worker_task.assert_not_called()
        assert not dedup.claimed

    async def test_incomplete_notifies_origin_channel(self):
        worker, notification = _dr_worker(response={
            "metadata": _MD2, "incomplete_details": {"reason": "max_output_tokens"}})
        with patch("src.handlers.worker_handler.deliver_deep_research", new_callable=AsyncMock) as deliver:
            _, status = await worker._dispatch(_dr_event("response.incomplete", job_id="r4"), {})
        assert status == 200
        kw = notification.notify.await_args.kwargs
        assert kw["channel_id_override"] == "C1" and kw["platform_override"] == "telegram"
        assert "incomplete" in kw["system_alert"]
        deliver.assert_not_called()


class TestEnqueueFailureNotice:
    async def _run(self, queue):
        from src.services.deep_research_delivery import deliver_deep_research
        notifier = AsyncMock()
        await deliver_deep_research(
            result_text="r", user_id="u", account_id="a", query="q", task_queue=queue,
            session_id="u:C1", channel_id_override="C1", platform_override="telegram",
            failure_notification=notifier)
        return notifier

    async def test_enqueue_failure_notifies_once_with_overrides(self):
        queue = AsyncMock()
        queue.enqueue_agent_task.side_effect = RuntimeError("tasks down")
        notifier = await self._run(queue)
        notifier.notify.assert_awaited_once()
        kw = notifier.notify.await_args.kwargs
        assert kw["channel_id_override"] == "C1" and kw["platform_override"] == "telegram"
        assert "tasks down" not in kw["system_alert"]

    async def test_enqueue_ok_does_not_notify(self):
        notifier = await self._run(AsyncMock())
        notifier.notify.assert_not_called()

    async def test_notice_failure_is_not_raised(self):
        queue = AsyncMock()
        queue.enqueue_agent_task.side_effect = RuntimeError("tasks down")
        from src.services.deep_research_delivery import deliver_deep_research
        notifier = AsyncMock()
        notifier.notify.side_effect = RuntimeError("slack down")
        await deliver_deep_research(result_text="r", user_id="u", account_id="a", query="q",
                                    task_queue=queue, failure_notification=notifier)
