"""DailyEmailReviewTask: one user's review, every branch of `__call__` (composition-root refactor S5ef).

The review service, the notifier, the agent builder, the reconnect flag and the ops sink are recording fakes.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.domain.agent import AgentStatus
from src.domain.exceptions import OAuthGrantRevokedError
from src.domain.notification_kind import NotificationKind
from src.domain.notify_result import NotifyResult, empty_answer_alert
from src.services import daily_email_review_task as module
from src.services.daily_email_review_task import DailyEmailReviewTask
from tests.support.clock_instants import pin_datetime

NOW = datetime(2026, 10, 22, 9, 5, tzinfo=timezone.utc)
EMAILS = [{"email_id": "e1", "subject": "Hello"}, {"email_id": "e2", "subject": "Invoice"}]
DELIVERED = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS, sent=True)
EMPTY = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="empty_answer")
FAILED = NotifyResult(delivered=False, agent_status=AgentStatus.TIMEOUT, error="timeout")
NO_ERROR = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED)
PAYLOAD = {"user_id": "user-1", "account_id": "acct-1"}


class Review:
    def __init__(self, fetched: Any) -> None:
        self.fetched = fetched
        self.fetch_calls: List[str] = []
        self.alert_args: List[Any] = []

    async def fetch_review_payload(self, user_id: str) -> Any:
        self.fetch_calls.append(user_id)
        if isinstance(self.fetched, Exception):
            raise self.fetched
        return self.fetched

    def build_alert(self, date_str: str, email_count: int) -> str:
        self.alert_args.append((date_str, email_count))
        return f"alert {date_str} {email_count}"


class Rig:
    def __init__(self, fetched: Any = EMAILS, result: NotifyResult = DELIVERED, sink_fails: Optional[bool] = False) -> None:
        self.order: List[str] = []
        self.notify_calls: List[Dict[str, Any]] = []
        self.flags: List[Any] = []
        self.posts: List[str] = []
        self.review = Review(fetched)
        self._result = result
        self._sink_fails = sink_fails
        self.task = DailyEmailReviewTask(
            email_review=self.review, ensure_agents=self._ensure, notification_service=self,  # type: ignore[arg-type]
            gmail_reconnect_flag=self, alert_sink=None if sink_fails is None else self)  # type: ignore[arg-type]

    async def _ensure(self, user_id: str) -> None:
        self.order.append(f"ensure:{user_id}")

    async def notify(self, **kwargs: Any) -> NotifyResult:
        self.order.append("notify")
        self.notify_calls.append(kwargs)
        return self._result

    async def flag(self, user_id: str, account_id: str) -> None:
        self.flags.append((user_id, account_id))

    async def post(self, text: str) -> None:
        if self._sink_fails:
            raise RuntimeError("webhook down")
        self.posts.append(text)


async def test_a_payload_without_user_or_account_is_a_400_and_touches_nothing():
    rig = Rig()

    assert await rig.task({"account_id": "acct-1"}, {}) == ({"error": "missing user_id or account_id"}, 400)
    assert await rig.task({"user_id": "user-1"}, {}) == ({"error": "missing user_id or account_id"}, 400)
    assert rig.review.fetch_calls == [] and rig.order == []


async def test_a_revoked_grant_flags_the_reconnect_and_ends_200_without_a_turn():
    rig = Rig(fetched=OAuthGrantRevokedError("invalid_grant"))

    assert await rig.task(PAYLOAD, {}) == ({"error": "gmail_reconnect_required"}, 200)
    assert rig.flags == [("user-1", "acct-1")]
    assert rig.order == []


async def test_missing_credentials_end_200_without_a_turn():
    rig = Rig(fetched=None)

    assert await rig.task(PAYLOAD, {}) == ({"error": "credentials unavailable"}, 200)
    assert rig.order == [] and rig.flags == []


async def test_an_empty_inbox_ends_200_without_a_turn(caplog):
    rig = Rig(fetched=[])

    with caplog.at_level(logging.INFO):
        assert await rig.task(PAYLOAD, {}) == ({"status": "no_emails"}, 200)

    assert rig.order == []
    assert "[Worker] daily_email_review: no emails in last 24h for user-1" in caplog.messages


async def test_a_delivered_review_builds_the_agents_first_then_notifies_with_the_alert_of_the_utc_date(monkeypatch, caplog):
    pin_datetime(monkeypatch, module, NOW)
    rig = Rig()

    with caplog.at_level(logging.INFO):
        assert await rig.task(PAYLOAD, {}) == ({"status": "ok", "emails": 2}, 200)

    assert rig.order == ["ensure:user-1", "notify"]
    assert rig.review.alert_args == [("2026-10-22", 2)]
    assert rig.notify_calls == [dict(
        kind=NotificationKind.DAILY_DIGEST, user_id="user-1", account_id="acct-1", system_alert="alert 2026-10-22 2",
        agent_id_override="smart_response_agent_user-1", save_history=False, framing_suffix="",
        thinking_effort="medium", task_complexity="deep_reasoning", email_for_triage=EMAILS,
        storage_class="email_review", suppress_transient_retry=True, origin_detail="daily_email_review")]
    assert "[Worker] daily_email_review: delivered 2 emails to Smart for user-1" in caplog.messages
    assert rig.posts == []


async def test_an_empty_answer_is_final_one_error_line_and_one_alert_without_content(caplog):
    rig = Rig(result=EMPTY)

    with caplog.at_level(logging.ERROR):
        assert await rig.task(PAYLOAD, {}) == ({"status": "empty_answer_final", "emails": 2}, 200)

    assert rig.posts == [empty_answer_alert("daily_email_review", "user-1", None)]
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "background_empty_answer"]
    assert (record.path, record.user_id) == ("daily_email_review", "user-1")


async def test_an_empty_answer_whose_alert_fails_is_still_final(caplog):
    rig = Rig(result=EMPTY, sink_fails=True)

    with caplog.at_level(logging.WARNING):
        assert await rig.task(PAYLOAD, {}) == ({"status": "empty_answer_final", "emails": 2}, 200)

    assert "[Worker] background_empty_answer alert failed: webhook down" in caplog.messages


async def test_an_empty_answer_without_an_ops_sink_is_still_final_and_logged(caplog):
    rig = Rig(result=EMPTY, sink_fails=None)

    with caplog.at_level(logging.ERROR):
        assert await rig.task(PAYLOAD, {}) == ({"status": "empty_answer_final", "emails": 2}, 200)

    assert len([r for r in caplog.records if getattr(r, "event", None) == "background_empty_answer"]) == 1


async def test_a_failed_delivery_answers_500_with_the_error_and_the_agent_status_so_the_queue_retries(caplog):
    rig = Rig(result=FAILED)

    with caplog.at_level(logging.WARNING):
        assert await rig.task(PAYLOAD, {}) == (
            {"error": "timeout", "agent_status": AgentStatus.TIMEOUT.value, "emails": 2}, 500)

    assert rig.posts == []
    assert any("delivery FAILED for user-1 (emails=2" in m for m in caplog.messages)


async def test_a_failed_delivery_without_an_error_names_it_delivery_failed():
    rig = Rig(result=NO_ERROR)

    body, status = await rig.task(PAYLOAD, {})

    assert (status, body["error"]) == (500, "delivery_failed")
