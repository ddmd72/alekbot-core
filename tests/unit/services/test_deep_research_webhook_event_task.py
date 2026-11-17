"""DeepResearchWebhookEventTask: one OpenAI research outcome, over the real sender and recording fakes."""
import logging
from typing import Any, Dict, List, Optional

import pytest

from src.domain import deep_research_notices as notices
from src.domain.agent import AgentStatus
from src.domain.notify_result import NotifyResult
from src.services.deep_research_notice_sender import DeepResearchNoticeSender
from src.services.deep_research_webhook_event_task import DeepResearchWebhookEventTask

PAYLOAD = {"event_type": "response.completed", "job_id": "job-1", "user_id": "user-1", "account_id": "acct-1",
           "session_id": "user-1:chan-1", "origin_platform": "telegram", "query": "tides"}


class Store:
    def __init__(self, order: List[str]) -> None:
        self.order = order
        self.records: List[Dict[str, Any]] = []

    async def record_dr_result(self, **kwargs: Any) -> None:
        self.order.append("store")
        self.records.append(kwargs)


class Rig:
    def __init__(self, response: Optional[dict] = None, fetch_raises: bool = False, handed_off: bool = True,
                 with_store: bool = True, delivered: bool = True) -> None:
        self.order: List[str] = []
        self.fetched: List[str] = []
        self.notices: List[Dict[str, Any]] = []
        self.reports: List[Dict[str, Any]] = []
        self._response = response if response is not None else {"output_text": "REPORT", "model": "model-1"}
        self._fetch_raises = fetch_raises
        self._handed_off = handed_off
        self._result = NotifyResult(delivered=delivered, agent_status=AgentStatus.SUCCESS, sent=delivered,
                                    error=None if delivered else "boom")
        self.store = Store(self.order) if with_store else None
        self.dispatch, self.media, self.links = object(), object(), object()
        self.task = DeepResearchWebhookEventTask(
            fetch=self._fetch, ensure_agents=self._ensure, prompt_content_store=self.store,  # type: ignore[arg-type]
            task_dispatch=self.dispatch, media_storage=self.media, link_service=self.links,  # type: ignore[arg-type]
            notification_service=self,  # type: ignore[arg-type]
            notice_sender=DeepResearchNoticeSender(notification_service=self, alert_sink=None),  # type: ignore[arg-type]
            deliver_report=self._deliver)

    async def _fetch(self, job_id: str) -> dict:
        self.fetched.append(job_id)
        if self._fetch_raises:
            raise RuntimeError("api down")
        return self._response

    async def _ensure(self, user_id: str) -> None:
        self.order.append(f"ensure:{user_id}")

    async def _deliver(self, **kwargs: Any) -> bool:
        self.order.append("deliver")
        self.reports.append(kwargs)
        return self._handed_off

    async def notify(self, **kwargs: Any) -> NotifyResult:
        self.order.append("notify")
        self.notices.append(kwargs)
        return self._result

    async def run(self, **payload: Any) -> Any:
        return await self.task({**PAYLOAD, **payload}, {})


@pytest.mark.parametrize("drop", ["event_type", "job_id", "user_id", "account_id"])
async def test_a_payload_missing_a_field_is_a_400_and_fetches_nothing(drop):
    rig = Rig()
    payload = {k: v for k, v in PAYLOAD.items() if k != drop}
    assert await rig.task(payload, {}) == ({"error": "missing event_type, job_id, user_id or account_id"}, 400)
    assert rig.fetched == [] and rig.order == []


async def test_a_retrieval_that_raises_is_a_500_for_a_retry(caplog):
    rig = Rig(fetch_raises=True)
    with caplog.at_level(logging.ERROR):
        assert await rig.run() == ({"error": "retrieval_failed"}, 500)
    assert "response retrieval failed: job=job-1 error=api down" in caplog.text
    assert rig.order == []


async def test_a_completed_job_stores_its_result_then_hands_the_report_to_the_origin_channel():
    rig = Rig()
    assert await rig.run() == ({"status": "ok"}, 200)
    assert rig.fetched == ["job-1"]
    assert rig.order == ["ensure:user-1", "store", "deliver"]
    assert rig.store.records == [dict(output_text="REPORT", query="tides", user_id="user-1", account_id="acct-1",
                                      model="model-1", provider="openai", source="openai_webhook", job_id="job-1")]
    assert rig.reports == [dict(result_text="REPORT", user_id="user-1", account_id="acct-1", query="tides",
                                task_queue=rig.dispatch, session_id="user-1:chan-1", media_storage=rig.media,
                                link_service=rig.links, channel_id_override="chan-1",
                                platform_override="telegram", failure_notification=rig, report_dedup_id="job-1")]
    assert rig.notices == []


async def test_a_completed_job_without_a_store_or_origin_is_still_delivered():
    rig = Rig(response={}, with_store=False)
    assert await rig.run(session_id="", origin_platform="", query="") == ({"status": "ok"}, 200)
    assert rig.reports[0]["result_text"] == "" and rig.reports[0]["channel_id_override"] is None
    assert rig.reports[0]["platform_override"] is None


async def test_a_report_task_that_was_not_enqueued_is_acked_not_retried(caplog):
    rig = Rig(handed_off=False)
    with caplog.at_level(logging.ERROR):
        assert await rig.run() == ({"status": "report_not_enqueued"}, 200)
    assert "report task not enqueued: job=job-1 user=user-1" in caplog.text


@pytest.mark.parametrize("event, response, alert, level, line", [
    ("response.failed", {"error": {"message": "quota"}}, notices.provider_error_notice("tides"), logging.ERROR,
     "job failed: job=job-1 error=quota"),
    ("response.failed", {}, notices.provider_error_notice("tides"), logging.ERROR,
     "job failed: job=job-1 error=unknown error"),
    ("response.incomplete", {"incomplete_details": {"reason": "max_output_tokens"}},
     notices.incomplete_notice("tides"), logging.WARNING, "job incomplete: job=job-1 reason=max_output_tokens"),
    ("response.incomplete", {}, notices.incomplete_notice("tides"), logging.WARNING,
     "job incomplete: job=job-1 reason=unknown"),
    ("response.cancelled", {}, notices.cancelled_notice("tides"), logging.INFO, "job cancelled: job=job-1"),
])
async def test_an_outcome_without_a_report_sends_its_notice_to_the_origin_channel(event, response, alert, level,
                                                                                 line, caplog):
    rig = Rig(response=response)
    with caplog.at_level(level):
        assert await rig.run(event_type=event) == ({"status": "ok"}, 200)
    assert line in caplog.text
    [call] = rig.notices
    assert call["system_alert"] == alert
    assert (call["channel_id_override"], call["platform_override"]) == ("chan-1", "telegram")
    assert rig.order == ["ensure:user-1", "notify"] and rig.reports == [] and rig.store.records == []


async def test_a_notice_that_reached_no_channel_is_a_500():
    assert await Rig(delivered=False).run(event_type="response.cancelled") == ({"error": "boom"}, 500)


async def test_an_unknown_event_type_is_a_400_after_the_agents_are_built(caplog):
    rig = Rig()
    with caplog.at_level(logging.ERROR):
        assert await rig.run(event_type="response.weird") == ({"error": "unknown event_type"}, 400)
    assert "unknown webhook event type 'response.weird': job=job-1" in caplog.text
    assert rig.order == ["ensure:user-1"] and rig.notices == []
