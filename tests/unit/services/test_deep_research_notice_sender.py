"""DeepResearchNoticeSender: one deep-research notice and the HTTP answer each notify outcome maps to."""
import logging
from typing import Any, Dict, List, Optional

import pytest

from src.domain.agent import AgentStatus
from src.domain.notification_kind import NotificationKind
from src.domain.notify_result import NotifyResult, empty_answer_alert
from src.services.deep_research_notice_sender import DeepResearchNoticeSender

DELIVERED = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS, sent=True)
EMPTY = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="empty_answer")
NOT_SENT = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="channel down")
SENT_NOT_CONFIRMED = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="timeout", sent=True)
NOT_SENT_NO_ERROR = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED)


class Notifier:
    def __init__(self, result: NotifyResult) -> None:
        self._result = result
        self.calls: List[Dict[str, Any]] = []

    async def notify(self, **kwargs: Any) -> NotifyResult:
        self.calls.append(kwargs)
        return self._result


class Sink:
    def __init__(self, fails: bool = False) -> None:
        self.posts: List[str] = []
        self._fails = fails

    async def post(self, text: str) -> None:
        if self._fails:
            raise RuntimeError("webhook down")
        self.posts.append(text)


async def _send(result: NotifyResult, sink: Optional[Sink] = None):
    notifier = Notifier(result)
    sender = DeepResearchNoticeSender(notification_service=notifier, alert_sink=sink)  # type: ignore[arg-type]
    answer = await sender.send("user-1", "acct-1", "the alert", "chan-1", "slack", context="event=x")
    return answer, notifier


async def test_a_delivered_notice_goes_to_the_origin_channel_without_agent_retry_and_answers_ok():
    answer, notifier = await _send(DELIVERED)
    assert answer == ({"status": "ok"}, 200)
    assert notifier.calls == [dict(kind=NotificationKind.DEEP_RESEARCH, user_id="user-1", account_id="acct-1",
                                   system_alert="the alert", channel_id_override="chan-1",
                                   platform_override="slack", suppress_transient_retry=True)]


async def test_a_notice_that_reached_no_channel_answers_500_for_a_retry(caplog):
    with caplog.at_level(logging.WARNING):
        assert (await _send(NOT_SENT))[0] == ({"error": "channel down"}, 500)
    assert "[DeepResearch] notice not sent: user=user-1 event=x error=channel down" in caplog.text


async def test_a_failure_without_an_error_text_is_named_delivery_failed():
    assert (await _send(NOT_SENT_NO_ERROR))[0] == ({"error": "delivery_failed"}, 500)


async def test_a_notice_sent_but_not_confirmed_is_acked_and_not_retried(caplog):
    with caplog.at_level(logging.WARNING):
        assert (await _send(SENT_NOT_CONFIRMED))[0] == ({"status": "ok"}, 200)
    assert "notice sent but not confirmed: user=user-1 event=x error=timeout; not retried" in caplog.text


async def test_an_empty_answer_is_final_with_one_error_line_and_one_ops_alert(caplog):
    sink = Sink()
    with caplog.at_level(logging.ERROR):
        assert (await _send(EMPTY, sink))[0] == ({"status": "empty_answer_final"}, 200)
    assert sink.posts == [empty_answer_alert("deep_research_notice", "user-1", None)]
    records = [r for r in caplog.records if getattr(r, "event", None) == "background_empty_answer"]
    assert len(records) == 1 and records[0].path == "deep_research_notice"


async def test_an_empty_answer_without_an_ops_sink_is_still_final(caplog):
    with caplog.at_level(logging.ERROR):
        assert (await _send(EMPTY, None))[0] == ({"status": "empty_answer_final"}, 200)
    assert len([r for r in caplog.records if getattr(r, "event", None) == "background_empty_answer"]) == 1


async def test_a_failing_ops_sink_does_not_fail_the_task(caplog):
    with caplog.at_level(logging.WARNING):
        assert (await _send(EMPTY, Sink(fails=True)))[0] == ({"status": "empty_answer_final"}, 200)
    assert "background_empty_answer alert failed: webhook down" in caplog.text
