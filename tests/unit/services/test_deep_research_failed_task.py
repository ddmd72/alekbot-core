"""DeepResearchFailedTask: the failure notice of one research job, over the real sender and a recording notifier."""
from typing import Any, Dict, List

import pytest

from src.domain import deep_research_notices as notices
from src.domain.agent import AgentStatus
from src.domain.notify_result import NotifyResult
from src.services.background_empty_answer_reporter import BackgroundEmptyAnswerReporter
from src.services.deep_research_failed_task import DeepResearchFailedTask
from src.services.deep_research_notice_sender import DeepResearchNoticeSender

PAYLOAD = {"user_id": "user-1", "account_id": "acct-1", "session_id": "user-1:chan-1", "query": "tides",
           "reason": "agent_failed", "origin_platform": "telegram"}


class Rig:
    def __init__(self, delivered: bool = True) -> None:
        self.order: List[str] = []
        self.calls: List[Dict[str, Any]] = []
        self._result = NotifyResult(delivered=delivered, agent_status=AgentStatus.SUCCESS, sent=delivered,
                                    error=None if delivered else "boom")
        self.task = DeepResearchFailedTask(
            ensure_agents=self._ensure, notice_sender=DeepResearchNoticeSender(notification_service=self,  # type: ignore[arg-type]
                                                                               empty_answer_reporter=BackgroundEmptyAnswerReporter(alert_sink=None)))

    async def _ensure(self, user_id: str) -> None:
        self.order.append(f"ensure:{user_id}")

    async def notify(self, **kwargs: Any) -> NotifyResult:
        self.order.append("notify")
        self.calls.append(kwargs)
        return self._result


@pytest.mark.parametrize("drop", ["user_id", "account_id"])
async def test_a_payload_without_its_ids_is_a_400_and_tells_nobody(drop):
    rig = Rig()
    payload = {k: v for k, v in PAYLOAD.items() if k != drop}
    assert await rig.task(payload, {}) == ({"error": "missing user_id or account_id"}, 400)
    assert rig.order == []


async def test_the_failure_notice_goes_once_to_the_origin_channel_after_the_agents_are_built():
    rig = Rig()
    assert await rig.task(dict(PAYLOAD), {}) == ({"status": "ok"}, 200)
    assert rig.order == ["ensure:user-1", "notify"]
    [call] = rig.calls
    assert call["system_alert"] == notices.research_failed_notice("tides")
    assert (call["channel_id_override"], call["platform_override"]) == ("chan-1", "telegram")


async def test_a_job_whose_report_could_not_be_delivered_gets_the_report_not_prepared_notice():
    rig = Rig()
    await rig.task({**PAYLOAD, "reason": "delivery_failed"}, {})
    assert rig.calls[0]["system_alert"] == notices.report_not_prepared_notice("tides")


async def test_an_old_payload_without_platform_session_or_query_has_no_overrides():
    rig = Rig()
    await rig.task({"user_id": "user-1", "account_id": "acct-1"}, {})
    call = rig.calls[0]
    assert call["channel_id_override"] is None and call["platform_override"] is None
    assert call["system_alert"] == notices.research_failed_notice("")


async def test_a_notice_that_reached_no_channel_is_a_500():
    assert (await Rig(delivered=False).task(dict(PAYLOAD), {})) == ({"error": "boom"}, 500)
